# oir-organoid-analysis

读取奥林巴斯 `.oir` 显微图像，自动分通道、分割，并计算类器官中各类细胞/通道的**面积占比**与**浸润深度**的交互式工具（基于 [napari](https://napari.org)）。

## 功能

- 读取 `.oir`（内部用 Bio-Formats 转为 OME-TIFF）
- 自动按通道拆分、识别染料名（如 DAPI / Alexa Fluor 488）
- 每通道 Otsu 自动分割，支持**手动阈值滑条**实时微调
- 手动涂改：画笔 / 擦除 / 多边形框选 / 填充
- 计算各通道**面积占比**（阳性面积 ÷ 组织区域）
- 计算各通道**浸润深度**（像素到表面距离 ÷ 最大内切圆半径）

## 依赖

- Python 3.9+
- **Java 运行时（JDK/JRE 17）** —— 用于 Bio-Formats 读 `.oir`（非 pip 包）

```bash
pip install -r requirements.txt
```

## 使用

```bash
# 打开后点「选择 .oir 文件」载入
python segment_interactive.py

# 或直接指定文件
python segment_interactive.py 你的文件.oir
```

载入后：

1. 右侧「阈值调节」面板拖动滑条，实时微调每个通道的阳性范围；
2. 手动涂改「组织区域(分母)」和各通道「阳性」掩膜（`1`=擦除 `2`=画笔 `3`=多边形 `4`=填充）；
3. 点「计算占比」或「计算浸润深度」查看结果。

## 打包成独立 exe（可选）

用 PyInstaller 打成免环境的独立文件夹（含 Java 与全部依赖）：

```bash
pyinstaller --onedir --console \
  --collect-all napari --collect-all skimage --collect-all vispy \
  --exclude-module PyQt6 \
  --add-data "<jre目录>;jre" \
  --add-data "<bioformats_package.jar>;." \
  --name segment_interactive segment_interactive.py
```

## 计算说明

**面积占比**：各通道阳性像素数 ÷ 组织区域（四通道阳性并集）像素数。

**浸润深度**：对每个阳性像素，算它到组织表面（边界）的欧氏距离，除以最大内切圆半径归一化到 [0,1]，再对通道取平均。

- 归一化深度 ≈ 0 → 细胞全在表面
- ≈ 1/3 → 均匀铺满组织
- → 1 → 集中在核心深处

## 注意

- 通道名显示的是**染料名**（DAPI / Alexa Fluor 488 等），标记名（CD31 / TNNT2 / Vimentin）需按实验设计自行映射。
- 「组织区域(分母)」是四通道阳性并集的近似，若需更准的组织轮廓，建议改用明场图。
