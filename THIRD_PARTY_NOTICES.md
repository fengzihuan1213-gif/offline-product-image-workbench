# 第三方组件与模型

工作台自有代码采用根目录的 MIT 许可证。以下组件、模型和相关上游代码适用各自许可。

| 组件 | 用途 | 上游许可与来源 |
|---|---|---|
| U²-Net | 主体分割模型 | Apache-2.0，[xuebinqin/U-2-Net](https://github.com/xuebinqin/U-2-Net) |
| rembg | U²-Net ONNX 权重发布来源及推理预处理参考 | MIT，Copyright (c) 2020 Daniel Gatis，[danielgatis/rembg](https://github.com/danielgatis/rembg) |
| ONNX Runtime | 本地 CPU 推理 | MIT，[microsoft/onnxruntime](https://github.com/microsoft/onnxruntime) |
| RapidOCR | 文字检测、方向分类与识别 | Apache-2.0，Copyright (c) 2021 RapidOCR Authors，[RapidAI/RapidOCR](https://github.com/RapidAI/RapidOCR) |
| PaddleOCR | RapidOCR 所用 PP-OCR 模型的上游 | Apache-2.0，[PaddlePaddle/PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) |
| Pillow | 图片读写、蒙版和合成 | MIT-CMU，[python-pillow/Pillow](https://github.com/python-pillow/Pillow) |
| NumPy | 图像数组运算 | BSD-3-Clause 及所含组件许可，[numpy/numpy](https://github.com/numpy/numpy) |
| OpenCV / opencv-python | 图像修补及 OCR 图像运算 | Apache-2.0 及 wheel 所附第三方许可，[opencv/opencv](https://github.com/opencv/opencv)、[opencv/opencv-python](https://github.com/opencv/opencv-python) |

## 分发说明

源码仓库包含自有工作台代码、第三方声明与许可证文本。Python 依赖、OCR 模型和 U²-Net 权重在首次安装时由用户从上游获取。

下载脚本获取 rembg 上游发布的 `u2net.onnx`，文件长度为 175997641 字节，SHA-256 为 `8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491`。权重的适用许可和使用范围应以模型上游发布说明为准。

本项目的 ONNX 推理代码以 rembg 的 U2NetSession 预处理和后处理规则为参考，并加入本地文件校验、离线运行、画布处理和用户修边。rembg 的 MIT 许可和 Apache-2.0 许可文本保存在 `licenses/`。

安装依赖时还会安装其传递依赖。重新分发完整运行环境、安装包或模型时，应保留安装包内对应的 LICENSE、NOTICE 和版权文件，并核对该分发物所含组件的条款。
