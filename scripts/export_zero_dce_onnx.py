"""Export the official Zero-DCE Epoch99 checkpoint to an OpenCV-friendly ONNX model."""

import argparse
from pathlib import Path
import sys

import torch
from torch import nn


class ZeroDCECurveNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.e_conv1 = nn.Conv2d(3, 32, 3, 1, 1)
        self.e_conv2 = nn.Conv2d(32, 32, 3, 1, 1)
        self.e_conv3 = nn.Conv2d(32, 32, 3, 1, 1)
        self.e_conv4 = nn.Conv2d(32, 32, 3, 1, 1)
        self.e_conv5 = nn.Conv2d(64, 32, 3, 1, 1)
        self.e_conv6 = nn.Conv2d(64, 32, 3, 1, 1)
        self.e_conv7 = nn.Conv2d(64, 24, 3, 1, 1)
        self.relu = nn.ReLU(inplace=False)

    def forward(self, value):
        x1 = self.relu(self.e_conv1(value))
        x2 = self.relu(self.e_conv2(x1))
        x3 = self.relu(self.e_conv3(x2))
        x4 = self.relu(self.e_conv4(x3))
        x5 = self.relu(self.e_conv5(torch.cat([x3, x4], 1)))
        x6 = self.relu(self.e_conv6(torch.cat([x2, x5], 1)))
        return torch.tanh(self.e_conv7(torch.cat([x1, x6], 1)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if not args.checkpoint.is_file():
        sys.exit("Official Zero-DCE checkpoint not found.")
    model = ZeroDCECurveNet()
    state = torch.load(args.checkpoint, map_location="cpu", weights_only=True)
    model.load_state_dict(state)
    model.eval()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(
        model, torch.zeros(1, 3, 256, 256), args.output,
        input_names=["image"], output_names=["curve_parameters"],
        dynamic_axes={"image": {2: "height", 3: "width"}, "curve_parameters": {2: "height", 3: "width"}},
        opset_version=17,
        dynamo=False,
    )
    print(f"Exported {args.output}")


if __name__ == "__main__":
    main()
