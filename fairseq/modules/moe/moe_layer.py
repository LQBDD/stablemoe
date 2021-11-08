# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

# NOTE: This is a mirror of the code in
# https://github.com/facebookresearch/fairscale/tree/master/fairscale/nn/moe

import logging
import time
from typing import TYPE_CHECKING, Any, Optional, Tuple, Union, cast

import torch
import os
import torch.distributed as dist
from torch import Tensor
from torch.cuda import Event as CudaEvent
from torch.nn import Module, ModuleList
from fairseq import distributed_utils
from .moe_communication import _AllToAll
from fairseq import parameter

if TYPE_CHECKING:
    Base = Module[Tensor]
else:
    Base = Module


logger = logging.getLogger(__name__)
layer_id = 0
# Gate Log structure:
# {batch_id: 
#           {Layer_id: "gates":{}}
# }
# 

def save_trace(gates, dispatch_mask, layer_id):
    # (S, E, C)
    token_to_expert = torch.sum(dispatch_mask, dim=2)
    output = {'gates': gates, 'token_to_expert': token_to_expert}
    rk = dist.get_rank()
    output_path = os.path.join(parameter.save_dir, "trace")
    if not os.path.exists(output_path):
        os.makedirs(output_path)
    output['rank'] = rk
    output['updates'] = parameter.num_updates
    output['layer_id'] = layer_id
    output_file = os.path.join(output_path, "switch_trace_rank_{}_updates_{}_layer_{}.pt".format(rk, parameter.num_updates, layer_id))

    if not os.path.exists(output_file):
        torch.save(output, output_file)

class MOELayer(Base):
    """MOELayer module which implements MixtureOfExperts as described in Gshard_.
    ::

        gate = Top2Gate(model_dim, num_experts)
        moe = MOELayer(gate, expert)
        output = moe(input)
        l_aux = moe.l_aux

    .. Gshard_: https://arxiv.org/pdf/2006.16668.pdf

    Args:
        gate (torch.nn.Module):
            gate network
        expert (torch.nn.Module):
            expert network
    """

    def __init__(self, gate: Module, experts: Union[Module, ModuleList], args, group: Optional[Any] = None, all2all_group: Optional[Any] = None) -> None:
        super().__init__()
        self.gate = gate
        if type(experts) == ModuleList:
            self.experts = cast(ModuleList, experts)
        else:
            self.experts = ModuleList([experts])
        self.expert_group = group if group is not None else distributed_utils.get_moe_group(args.moe_expert_count)
        self.all2all_group = all2all_group if all2all_group is not None else distributed_utils.get_all2all_group(args.moe_expert_count)
        for p in experts.parameters():
            p.expert = True  # type: ignore
        self.world_size = distributed_utils.get_world_size(self.expert_group)
        self.all2all_size = distributed_utils.get_world_size(self.all2all_group)
        self.num_local_experts = len(self.experts)
        self.args = args
        self.in_generation = False
        self.a2a_cuda_event_intervals = []
        self.a2a_cpu_time_ms = 0.0
        global layer_id
        self.layer_id = layer_id
        layer_id += 1

    def forward(self, *input: Tensor, input_padding_mask=None, **kwargs: Any) -> Tensor:
        assert len(input) == 1, "only single input Tensor supported"
        input = input[0]
        assert len(input.shape) == 3, "input Tensor must have dimensions: (s)equence, (t)oken, (m)odel"
        if input_padding_mask is not None:
            assert len(input_padding_mask.shape) == 2, "input Tensor must have dimensions: (s)equence, (t)oken"
            assert input_padding_mask.shape[0] == input.shape[0]
            assert input_padding_mask.shape[1] == input.shape[1]
        # assert input.shape[0] % len(self.experts) == 0, "num tokens must be order of number of local experts"
        # Implement Algorithm 2 from GShard paper.
        d_model = input.shape[2]
        # Pad to expected batch size
        input_shape = list(input.shape)
        expected_bsz = getattr(self.args, 'batch_size', 0) if self.training else getattr(self.args, 'batch_size_valid', 0)
        # This indicates that --batch-size or --max-sentences is not specified
        if expected_bsz is None:
            expected_bsz = 0
        # Note: Padding is not necessary at generation time at present
        # because all DDP workers process the same batch. Also, batch size at generation time
        # can be different from that present in the checkpoint state
        if not self.in_generation and expected_bsz != 0 and input_shape[0] != expected_bsz:
            logger.warning(f"padding batch with unexpected size {input_shape[0]} (expected: {expected_bsz})")
            assert input_shape[0] < expected_bsz, f"{input_shape[0]} < {expected_bsz}"
            padded_input = torch.zeros(
                (expected_bsz, input_shape[1], input_shape[2]),
                dtype=input.dtype, layout=input.layout, device=input.device)
            padded_input[:input_shape[0], :, :] = input
            input = padded_input

            padded_input_padding_mask = torch.ones(
                (expected_bsz, input_shape[1], ), dtype=torch.bool, device=input.device
            )
            if input_padding_mask is not None:
                padded_input_padding_mask[:input_shape[0], :] = input_padding_mask
            else:
                padded_input_padding_mask[:input_shape[0], :] = False
            input_padding_mask = padded_input_padding_mask

        # Reshape into S tokens by dropping sequence dimension.
        reshaped_input = input.reshape(-1, d_model)
        reshaped_input_shape = reshaped_input.shape
        reshaped_input_padding_mask = input_padding_mask.reshape(-1) if input_padding_mask is not None else None

        # Doing padding here when --max-tokens is specified and not --batch-size or --max-sentences
        # Pro of --max-tokens: more flexible for MT variable sequence lengths
        # Con of --max-tokens: extra all-reduce needed to figure out optimal padding without running OOM
        if expected_bsz == 0:
            expected_dim = int(distributed_utils.all_reduce(
                reshaped_input_shape[0] * torch.ones((1,), dtype=torch.long, device=input.device),
                group=dist.group.WORLD,
                op="max",
            ).item())
            padded_input = torch.zeros(
                (expected_dim, reshaped_input_shape[1]),
                dtype=input.dtype, layout=input.layout, device=input.device)
            padded_input[:reshaped_input_shape[0], :] = reshaped_input
            reshaped_input = padded_input

            padded_input_padding_mask = torch.ones(
                (expected_dim,), dtype=torch.bool, device=padded_input.device
            )
            if reshaped_input_padding_mask is not None:
                padded_input_padding_mask[:reshaped_input_shape[0]] = reshaped_input_padding_mask
            else:
                padded_input_padding_mask[:reshaped_input_shape[0]] = False
            reshaped_input_padding_mask = padded_input_padding_mask
        # combine_weights, dispatch_mask: (tokens, num_experts, capacity)
        l_aux, combine_weights, dispatch_mask, self.metadata = self.gate(reshaped_input, reshaped_input_padding_mask)

        if parameter.save_trace:
            save_trace(self.metadata["gate_weights"], dispatch_mask, self.layer_id)

        dispatch_mask = dispatch_mask.to(input.dtype).permute(1, 2, 0)  # S,E,C -> E,C,S
        E, C, S = dispatch_mask.size()
        M = reshaped_input.size(1)
        assert reshaped_input.size() == (S, M)
        # einsum("sec,sm->ecm")
        dispatched_input = torch.mm(dispatch_mask.view(E*C, S), reshaped_input)  # -> (E*C),M
        if self.all2all_size > 1:
            dispatched_input = self.all_to_all_wrapper(dispatched_input)

        # Re-shape after all-to-all: ecm -> gecm
        dispatched_input = dispatched_input.reshape(self.all2all_size, self.num_local_experts, -1, d_model)
        chunks = dispatched_input.chunk(self.num_local_experts, dim=1)
        expert_outputs = []
        for chunk, expert in zip(chunks, self.experts):
            expert_outputs += [expert(chunk)]
        expert_output = torch.cat(expert_outputs, dim=1)

        if self.all2all_size > 1:
            expert_output = self.all_to_all_wrapper(expert_output)

        # Re-shape back: gecm -> ecm
        expert_output = expert_output.reshape(self.all2all_size * self.num_local_experts, -1, d_model)
        # einsum("sec,ecm->sm")
        combined_output = combine_weights.view(S, E*C).mm(expert_output.view(E*C, M))

        # Remove padding here when --max-tokens is specified and not --batch-size or --max-sentences
        combined_output = combined_output[:reshaped_input_shape[0], :]
        combined_output = combined_output.reshape(input.shape)
        combined_output = combined_output[:input_shape[0], :, :]

        self.record_all_to_all_stats()

        return combined_output, l_aux

    def prepare_for_inference_(self):
        self.in_generation = True

    def all_to_all_wrapper(self, input: Tensor):
        dummy_a2a = getattr(self.args, 'dummy_a2a', False)
        if dummy_a2a:
            input = input.contiguous()
            output = input.detach().clone()
            return input
        # always record times, since it is not a lot of overhead
        # if we do not log it we simply clear it off in record_all_to_all_stats
        cuda_start = torch.cuda.Event(enable_timing=True)
        cuda_end = torch.cuda.Event(enable_timing=True)
        cpu_start = time.time() * 1000
        cuda_start.record()
        output = _AllToAll.apply(self.all2all_group, input)
        cuda_end.record()
        cpu_end = time.time() * 1000
        self.a2a_cpu_time_ms += (cpu_end - cpu_start)
        self.a2a_cuda_event_intervals.append((cuda_start, cuda_end))
        return output

    def record_all_to_all_stats(self):
        # controlled via an argument as we want to minimize any impact from torch.cuda.synchronize()
        record_a2a_perf_stats = getattr(self.args, 'record_a2a_perf_stats', False)
        if record_a2a_perf_stats:
            torch.cuda.synchronize()
            self.metadata["all_to_all_cpu_time_ms"] = self.a2a_cpu_time_ms
            a2a_cuda_time_ms = 0.0
            for ev_start, ev_end in self.a2a_cuda_event_intervals:
                a2a_cuda_time_ms += ev_start.elapsed_time(ev_end)
            self.metadata["all_to_all_cuda_time_ms"] = a2a_cuda_time_ms
        # reset stats
        self.a2a_cpu_time_ms = 0.0
        self.a2a_cuda_event_intervals = []
