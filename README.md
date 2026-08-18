# PDF 红章移除工具

这是一个命令行 Python 脚本，用于检测 PDF 中一个或多个红色印章，并把检测到的红色区域修补为邻近底色，同时尽量保留页面中的其他文字、图片、矢量对象、页面尺寸和页数。

## 功能

- 处理独立的签章/印章批注层
- 处理已经压平到页面内容中的红色印章
- 支持多页 PDF 和同页多个印章
- 默认只识别具有印章尺寸特征的红色区域
- 支持指定页码、调试输出和检测阈值调整
- 输出仍为 PDF，默认保存在输入文件旁边

## 安装

需要 Python 3.10 或更高版本。

```bash
python -m pip install -r requirements.txt
```

## 使用

最简单的用法：

```bash
python remove_red_stamps.py input.pdf
```

默认输出文件名为 `input_去除红章.pdf`。

指定输出文件：

```bash
python remove_red_stamps.py input.pdf -o output.pdf
```

只处理指定页面：

```bash
python remove_red_stamps.py input.pdf --pages 1,3-5
```

保存检测过程中的渲染图、掩膜和检测框：

```bash
python remove_red_stamps.py input.pdf --debug-dir debug
```

允许覆盖已有输出：

```bash
python remove_red_stamps.py input.pdf -o output.pdf --overwrite
```

查看全部参数：

```bash
python remove_red_stamps.py --help
```

## 常用参数

| 参数 | 作用 |
| --- | --- |
| `--mode auto` | 默认模式，同时处理批注层和压平红章 |
| `--mode annotations-only` | 只处理独立签章/印章批注 |
| `--mode flattened-only` | 只处理已压平到页面中的红章 |
| `--pages 1,3-5` | 只处理指定页面 |
| `--dpi 240` | 设置检测分辨率，范围为 72–600 DPI |
| `--min-size-mm 7` | 设置自动识别区域的最小宽度和高度 |
| `--fill-mode background` | 使用局部主底色修补 |
| `--fill-mode inpaint` | 使用邻近像素传播修补 |
| `--all-red` | 处理所有红色像素，可能误删红色文字或图形 |
| `--debug-dir debug` | 输出调试图，便于检查检测范围 |

## 处理方式

脚本优先移除独立的红色签章批注层，因为这种方式最有可能完整保留下方内容。对于页面内容中的红色矢量对象，脚本尝试只删除检测框内的红色对象。无法分离的压平图像红章则通过透明修补层覆盖检测到的红色像素。

## 注意事项

- 请始终保留原始 PDF，并先在副本上检查处理结果。
- 任何 PDF 修改都可能使原有数字签名失效。
- 已压平红章遮住的原始像素或文字不存在于文件中时，程序无法真正恢复，只能根据邻近底色修补。
- 页面原本包含红色文字或图形时，请先使用 `--debug-dir` 检查；谨慎使用 `--all-red`。
- 仅处理你拥有或已获授权修改的文档，不要将修改后的文件冒充未经修改的官方文件。
