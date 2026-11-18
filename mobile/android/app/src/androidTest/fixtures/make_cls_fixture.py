"""Regenerate NativeEmbeddingsTest's tiny ONNX input.

Run with onnx==1.17.0. This is an actual tensor graph, not a mock runtime:
output[b,t,:] = input_ids[b,t] + attention_mask[b,t] + 2*token_type_ids[b,t]
                + [3,4,0,...,0].
The test verifies the adapter selects CLS (token zero) and normalizes it.
"""
import base64
import gzip

import onnx
from onnx import TensorProto, helper

inputs = [helper.make_tensor_value_info(name, TensorProto.INT64, ["batch", "tokens"])
          for name in ("input_ids", "attention_mask", "token_type_ids")]
initializers = [
    helper.make_tensor("two", TensorProto.INT64, [], [2]),
    helper.make_tensor("axis", TensorProto.INT64, [1], [2]),
    helper.make_tensor("basis", TensorProto.FLOAT, [1, 1, 384], [3.0, 4.0] + [0.0] * 382),
]
nodes = [
    helper.make_node("Mul", ["token_type_ids", "two"], ["typed"]),
    helper.make_node("Add", ["input_ids", "typed"], ["ids_types"]),
    helper.make_node("Add", ["ids_types", "attention_mask"], ["all_ids"]),
    helper.make_node("Cast", ["all_ids"], ["floats"], to=TensorProto.FLOAT),
    helper.make_node("Unsqueeze", ["floats", "axis"], ["expanded"]),
    helper.make_node("Add", ["expanded", "basis"], ["last_hidden_state"]),
]
output = helper.make_tensor_value_info("last_hidden_state", TensorProto.FLOAT, ["batch", "tokens", 384])
graph = helper.make_graph(nodes, "lastphone-cls-adapter-test", inputs, [output], initializers)
model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)], ir_version=8,
                          producer_name="Lastphone instrumentation fixture")
onnx.checker.check_model(model)
print(base64.b64encode(gzip.compress(model.SerializeToString(), mtime=0)).decode("ascii"))
