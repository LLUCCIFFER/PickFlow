# 片刻

本地照片筛选与韩式成片导出工具。当前版本只保留 **极速模式**：照片分析、初筛、分组和选片都在本机完成，不上传照片，也不需要外部账号。

## 功能

- 自动扫描照片文件夹，读取 JPG、PNG、HEIC、WEBP、TIFF、RAW 等格式。
- 使用 pHash、dHash、wHash、aHash、HSV、ORB 等本地算法，把相似照片自动分组。
- 自动初筛明显失焦、曝光异常、构图异常或无法读取的照片。
- 在网页里左右对比选片，支持键盘快捷操作、撤销、跳过和重新处理。
- 将最终照片整理到 `winners/`，淘汰照片整理到 `losers/`。
- 在完成页为 `winners/` 批量添加韩式大头贴边框、贴纸或清透滤镜，输出到 `winners/kstyle_<时间戳>/`。

## 快速开始

### Windows

双击 `启动_Windows.bat`。

### macOS

双击 `启动_macOS.command`。如系统拦截，按住 Control 点击脚本，再选择打开。

### 手动启动

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

macOS / Linux 激活环境：

```bash
source .venv/bin/activate
```

默认访问地址：

```text
http://localhost:5057
```

## 操作说明

1. 在首页选择或粘贴照片文件夹路径。
2. 点击“开始”，等待本地扫描、初筛和分组完成。
3. 复核系统建议放手的照片；想保留的照片点一下即可召回。
4. 进入左右对比页面，选择更想留下的一张。
5. 完成后打开 `winners/` 查看最终照片。
6. 点击“韩式大头贴”，选择喜欢的风格，预览后批量导出网感成片。

## 快捷键

- `←`：保留左侧照片
- `→`：保留右侧照片
- `↑`：两张都保留
- `↓`：两张都放弃
- `[` / `]`：单独放弃左侧 / 右侧
- `S`：跳过当前组，稍后再处理
- `Z`：切换缩放
- `Shift + Z`：撤销当前组上一步

## 输出目录

```text
照片文件夹/
├── winners/                 # 最终保留照片
├── losers/                  # 淘汰照片
├── winners/kstyle_时间戳/    # 韩式大头贴/滤镜导出副本
├── .pic_selecter_state.json # 进度状态
└── _pic_selecter/           # 缩略图、缓存和日志
```

## 说明

- 极速模式全程本地运行，不上传照片。
- “移动”模式会把原文件移动到 `winners/` 与 `losers/`。
- “复制”模式会保留原文件，并在结果目录生成副本。
- 韩式大头贴/滤镜导出只生成新副本，不覆盖原始 winners。
