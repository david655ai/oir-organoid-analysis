# -*- coding: utf-8 -*-
"""
读入 .oir / .vsi / .tiff → 自动分通道 → 分割精调（带文件选择按钮）
==================================================================
运行：python segment_interactive.py
      （可选加参数直接指定：python segment_interactive.py xx.oir / xx.vsi / xx.tif）

窗口打开后，点右侧「选择 .oir 文件 / 选择 .vsi 文件 / 选择 TIFF 文件」按钮
-> 弹文件对话框选文件 -> 自动分通道（.oir / .vsi 会自动先转成 OME-TIFF）。

涂改：选中标签层后  1=擦除 2=画笔 3=多边形(单击加点,双击闭合) 4=填充
"""
import sys
import os
import re
import subprocess

import numpy as np
import tifffile
import napari
from skimage.filters import threshold_otsu
from scipy.ndimage import distance_transform_edt
from qtpy.QtWidgets import (QWidget, QVBoxLayout, QPushButton, QLabel,
                            QSlider, QFileDialog, QComboBox)
from qtpy.QtCore import Qt
from qtpy.QtGui import QFont

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))


def _locate():
    """定位 java 和 bioformats jar（打包模式 vs 开发模式）"""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        return (os.path.join(base, "jre", "bin", "java.exe"),
                os.path.join(base, "bioformats_package.jar"))
    import bioformats_jar
    return (os.path.join(os.path.dirname(sys.executable), "Library", "bin", "java.exe"),
            bioformats_jar.JAR)


JAVA, JAR = _locate()


def extract_dye_names(oir_path):
    """从 .oir 元数据提取染料名（按通道顺序去重）"""
    data = open(oir_path, "rb").read()
    names = re.findall(rb"<lsmimage:dyeName>([^<]*)</lsmimage:dyeName>", data)
    seen = []
    for n in names:
        s = n.decode("utf-8", "replace")
        if s not in seen:
            seen.append(s)
    return seen


def extract_dye_names_from_tiff(tiff_path):
    """从 OME-TIFF 元数据提取染料名（channel dyeName #N，按通道编号排序）"""
    t = tifffile.TiffFile(tiff_path)
    xml = t.ome_metadata or ""
    pairs = re.findall(r"channel dyeName #(\d+)</Key><Value>([^<]*)</Value>", xml)
    pairs = sorted((int(i), n) for i, n in pairs)
    return [n for _, n in pairs]


def to_ome_tiff(src_path):
    """Bio-Formats 把 .oir / .vsi 转成 OME-TIFF（已存在则复用）"""
    tiff_path = os.path.splitext(src_path)[0] + ".ome.tiff"
    if os.path.exists(tiff_path):
        return tiff_path
    print(f"正在转换 {os.path.basename(src_path)} -> OME-TIFF（首次较慢）...")
    cmd = [JAVA, "-cp", JAR, "loci.formats.tools.ImageConverter",
           "-overwrite", src_path, tiff_path]
    subprocess.run(cmd, check=True)
    print("转换完成：", tiff_path)
    return tiff_path


def get_pixel_size(tiff_path):
    """从 OME-TIFF 读物理像素尺寸（μm/px）"""
    t = tifffile.TiffFile(tiff_path)
    xml = t.ome_metadata or ""
    m = re.search(r'PhysicalSizeX="([\d.eE+-]+)"', xml)
    return float(m.group(1)) if m else None


def main():
    viewer = napari.Viewer(title="分割精调")

    result_label = QLabel("请点右侧「选择 .oir / .vsi / TIFF 文件」载入数据")
    result_label.setFont(QFont("Consolas", 10))

    # 动态状态（载入文件后填充）
    state = {"labels": {}, "names": [], "n_ch": 0, "pixel_um": None}

    # 通道占比（X 占 Y）面板
    ratio_panel = QWidget()
    ratio_layout = QVBoxLayout(ratio_panel)
    ratio_layout.addWidget(QLabel("通道占比（X 占 Y）"))
    ratio_layout.addWidget(QLabel("分子 X："))
    combo_x = QComboBox()
    ratio_layout.addWidget(combo_x)
    ratio_layout.addWidget(QLabel("分母 Y："))
    combo_y = QComboBox()
    ratio_layout.addWidget(combo_y)
    btn_ratio = QPushButton("计算通道占比")
    ratio_layout.addWidget(btn_ratio)
    ratio_result = QLabel("（载入文件后可计算）")
    ratio_result.setFont(QFont("Consolas", 10))
    ratio_layout.addWidget(ratio_result)

    # 阈值调节面板（每个通道一个滑条）
    threshold_panel = QWidget()
    threshold_layout = QVBoxLayout(threshold_panel)
    threshold_layout.addWidget(QLabel("阈值调节（拖滑条实时更新阳性）"))

    def rebuild_thresholds(bg_subs, names, ths):
        # 清空旧滑条
        while threshold_layout.count():
            item = threshold_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        threshold_layout.addWidget(QLabel("阈值调节（拖滑条实时更新阳性）"))
        for i in range(len(names)):
            lbl = QLabel(f"{names[i]}  阈值: {int(ths[i])}")
            sld = QSlider(Qt.Horizontal)
            sld.setRange(0, max(int(bg_subs[i].max()), 1))
            sld.setValue(int(ths[i]))

            def make_cb(idx, bg, nm, llbl):
                def cb(val):
                    state["labels"][f"ch{idx}"].data = (bg > val).astype(np.uint8)
                    llbl.setText(f"{nm}  阈值: {val}")
                return cb

            sld.valueChanged.connect(make_cb(i, bg_subs[i], names[i], lbl))
            threshold_layout.addWidget(lbl)
            threshold_layout.addWidget(sld)

    def rebuild_channel_combos(names):
        combo_x.clear()
        combo_y.clear()
        combo_x.addItems(names)
        combo_y.addItems(names)

    def compute_channel_ratio():
        """计算通道 X 占通道 Y 的占比（含 X∩Y 重叠比例）"""
        if not state["labels"]:
            ratio_result.setText("请先载入文件")
            return
        ix, iy = combo_x.currentIndex(), combo_y.currentIndex()
        if ix < 0 or iy < 0:
            ratio_result.setText("请先载入文件")
            return
        labels, names = state["labels"], state["names"]
        X = labels[f"ch{ix}"].data.astype(bool)
        Y = labels[f"ch{iy}"].data.astype(bool)
        nx, ny = int(X.sum()), int(Y.sum())
        nov = int((X & Y).sum())
        lines = [f"分子 X：{names[ix]}  阳性 = {nx} px",
                 f"分母 Y：{names[iy]}  阳性 = {ny} px",
                 ""]
        if ny == 0:
            lines.append("分母 Y 阳性为 0，无法计算占比")
        else:
            lines.append(f"X / Y = {nx} / {ny} = {100*nx/ny:.2f}%")
            lines.append(f"X∩Y / Y = {nov} / {ny} = {100*nov/ny:.2f}%（Y 中被 X 覆盖的比例）")
        ratio_result.setText("\n".join(lines))

    btn_ratio.clicked.connect(compute_channel_ratio)

    def load_tiff(tiff_path, dye=None, display_path=None):
        """读入 TIFF（.ome.tiff / 普通 .tif，含 RGB 彩色图）并构建图层；dye 为染料名列表"""
        a = tifffile.imread(tiff_path)
        # RGB 彩色图 (H,W,3/4) → 转成 (C,H,W) 灰度通道
        is_rgb = a.ndim == 3 and a.shape[-1] in (3, 4)
        if is_rgb:
            a = np.moveaxis(a, -1, 0)
        if a.ndim == 2:
            a = a[None]
        n_ch = a.shape[0]
        pixel_um = get_pixel_size(tiff_path) or 1.0
        if is_rgb:
            names = ["R", "G", "B", "A"][:n_ch]
        else:
            dye = dye or []
            names = [f"CH{i+1} {dye[i]}" if i < len(dye) else f"CH{i+1}" for i in range(n_ch)]

        # 减背景 + Otsu 自动分割
        bg_subs, masks, ths = [], [], []
        for i in range(n_ch):
            c = np.clip(a[i].astype(np.float64) - np.percentile(a[i], 1), 0, None)
            bg_subs.append(c)
            th = threshold_otsu(c)
            ths.append(th)
            masks.append(c > th)
        tissue = np.zeros_like(masks[0])
        for m in masks:
            tissue |= m

        # 清空旧图层，载入新图层
        viewer.layers.clear()
        for i in range(n_ch):
            viewer.add_image(a[i], name=names[i], colormap="gray")
        labels = {}
        labels["tissue"] = viewer.add_labels(tissue.astype(np.uint8), name="组织区域(分母)", opacity=0.4)
        labels["tissue"].selected_label = 1
        for i in range(n_ch):
            labels[f"ch{i}"] = viewer.add_labels(masks[i].astype(np.uint8), name=f"{names[i]} 阳性", opacity=0.4)
            labels[f"ch{i}"].selected_label = 1

        state.update(labels=labels, names=names, n_ch=n_ch, pixel_um=pixel_um, bg_subs=bg_subs)
        rebuild_thresholds(bg_subs, names, ths)
        rebuild_channel_combos(names)
        result_label.setText(f"已载入 {os.path.basename(display_path or tiff_path)}\n{len(names)} 个通道，可开始涂改")
        print("已载入：", display_path or tiff_path, " 通道：", names)

    def load_image(path):
        """按扩展名分发：.oir/.vsi 先转 OME-TIFF，.tif/.tiff 直接读"""
        ext = os.path.splitext(path)[1].lower()
        if ext == ".oir":
            tiff_path = to_ome_tiff(path)
            load_tiff(tiff_path, dye=extract_dye_names(path), display_path=path)
        elif ext == ".vsi":
            tiff_path = to_ome_tiff(path)
            load_tiff(tiff_path, dye=extract_dye_names_from_tiff(tiff_path), display_path=path)
        else:  # .tif / .tiff
            load_tiff(path, dye=extract_dye_names_from_tiff(path), display_path=path)

    def select_oir():
        path, _ = QFileDialog.getOpenFileName(None, "选择 .oir 文件", HERE, "Olympus OIR (*.oir)")
        if path:
            load_image(path)

    def select_tiff():
        path, _ = QFileDialog.getOpenFileName(None, "选择 TIFF 文件", HERE, "TIFF 文件 (*.tif *.tiff)")
        if path:
            load_image(path)

    def select_vsi():
        path, _ = QFileDialog.getOpenFileName(None, "选择 .vsi 文件", HERE, "Olympus VSI (*.vsi)")
        if path:
            load_image(path)

    def compute():
        if not state["labels"]:
            result_label.setText("请先载入文件")
            return
        labels, names, n_ch, pixel_um = state["labels"], state["names"], state["n_ch"], state["pixel_um"]
        t = labels["tissue"].data.astype(bool)
        tot = int(t.sum())
        lines = [f"组织区域(分母) = {tot} px"]
        if pixel_um:
            lines.append(f"= {tot*pixel_um**2:,.0f} μm²")
        lines.append("")
        for i in range(n_ch):
            px = int(labels[f"ch{i}"].data.astype(bool).sum())
            lines.append(f"{names[i]:24s} {100*px/tot:6.2f}%")
        result_label.setText("\n".join(lines))

    def compute_infiltration():
        """计算各通道浸润深度（像素到表面距离 / 最大内切圆半径，取平均）"""
        if not state["labels"]:
            result_label.setText("请先载入文件")
            return
        labels, names, n_ch, pixel_um = state["labels"], state["names"], state["n_ch"], state["pixel_um"]
        tissue = labels["tissue"].data.astype(bool)
        if tissue.sum() == 0:
            result_label.setText("组织区域为空，无法计算")
            return

        dist = distance_transform_edt(tissue)      # 每像素到表面的最短距离
        radius = dist.max()                        # 最大内切圆半径
        norm = dist / radius                       # 归一化深度 [0,1]

        lines = [f"半径(最大内切圆) = {radius:.0f} px"]
        if pixel_um:
            lines[-1] += f" = {radius*pixel_um:.1f} μm"
        lines += ["", "通道          平均深度  深区占比(>0.5)"]
        for i in range(n_ch):
            inside = labels[f"ch{i}"].data.astype(bool) & tissue
            if inside.sum() == 0:
                lines.append(f"{names[i]:20s} 无阳性")
                continue
            m = norm[inside].mean()
            deep = (norm[inside] > 0.5).mean() * 100
            lines.append(f"{names[i]:20s} {m:.3f}     {deep:5.1f}%")

        lines += ["", "深度分布(0-0.2|0.2-0.4|0.4-0.6|0.6-0.8|0.8-1.0)："]
        bins = [0, 0.2, 0.4, 0.6, 0.8, 1.0001]
        for i in range(n_ch):
            inside = labels[f"ch{i}"].data.astype(bool) & tissue
            if inside.sum() == 0:
                continue
            h, _ = np.histogram(norm[inside], bins=bins)
            p = h / inside.sum() * 100
            lines.append(f"  {names[i]:18s}" + "".join(f"{x:6.0f}%" for x in p))

        result_label.setText("\n".join(lines))

        # 深度热力图（napari 图层：表面=蓝 -> 中心=红）
        depth_map = np.where(tissue, norm, 0).astype(np.float32)
        dl = state.get("depth_layer")
        if dl is not None and dl in viewer.layers:
            dl.data = depth_map
        else:
            state["depth_layer"] = viewer.add_image(
                depth_map, name="浸润深度图", colormap="turbo", opacity=0.5)

    def save():
        if not state["labels"]:
            result_label.setText("请先载入文件")
            return
        labels, n_ch = state["labels"], state["n_ch"]
        np.savez("refined_masks.npz",
                 tissue=labels["tissue"].data.astype(bool),
                 channels=np.stack([labels[f"ch{i}"].data.astype(bool) for i in range(n_ch)]))
        result_label.setText("已保存 refined_masks.npz")

    def undo():
        active = viewer.layers.selection.active
        if active is not None and hasattr(active, "undo"):
            active.undo()
        else:
            print("请先点选一个标签层再撤销")

    def redo():
        active = viewer.layers.selection.active
        if active is not None and hasattr(active, "redo"):
            active.redo()
        else:
            print("请先点选一个标签层")

    # ---- 右侧「操作」面板 ----
    panel = QWidget()
    layout = QVBoxLayout(panel)
    layout.addWidget(QLabel("操作"))
    btn_select = QPushButton("选择 .oir 文件")
    btn_select.clicked.connect(select_oir)
    btn_tiff = QPushButton("选择 TIFF 文件")
    btn_tiff.clicked.connect(select_tiff)
    btn_vsi = QPushButton("选择 .vsi 文件")
    btn_vsi.clicked.connect(select_vsi)
    btn_compute = QPushButton("计算占比")
    btn_compute.clicked.connect(compute)
    btn_depth = QPushButton("计算浸润深度")
    btn_depth.clicked.connect(compute_infiltration)
    btn_save = QPushButton("保存结果")
    btn_save.clicked.connect(save)
    btn_undo = QPushButton("撤销 (Ctrl+Z)")
    btn_undo.clicked.connect(undo)
    btn_redo = QPushButton("重做 (Ctrl+Shift+Z)")
    btn_redo.clicked.connect(redo)
    layout.addWidget(btn_select)
    layout.addWidget(btn_tiff)
    layout.addWidget(btn_vsi)
    layout.addWidget(btn_compute)
    layout.addWidget(btn_depth)
    layout.addWidget(btn_save)
    layout.addWidget(btn_undo)
    layout.addWidget(btn_redo)
    layout.addWidget(result_label)
    layout.addStretch()
    viewer.window.add_dock_widget(panel, name="操作", area="right")
    viewer.window.add_dock_widget(threshold_panel, name="阈值调节", area="right")
    viewer.window.add_dock_widget(ratio_panel, name="通道占比", area="right")

    viewer.bind_key("c", lambda v: compute())
    viewer.bind_key("s", lambda v: save())

    # 若命令行给了文件，直接载入（按扩展名自动区分 .oir / .vsi / .tif）
    if len(sys.argv) > 1:
        load_image(sys.argv[1])

    print("提示：点右侧「选择 .oir / .vsi / TIFF 文件」按钮载入数据；1擦 2画 3多边形 4填充")
    napari.run()


if __name__ == "__main__":
    main()
