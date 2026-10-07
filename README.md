# 流水线拼豆 MVP(第一阶段 · 纯软件验证)

跑通全案第一阶段的算法主链:**图片 -> 降噪 -> 色卡量化 -> BOM -> PNG/PDF 图纸**。

## 依赖
仅 `Pillow`。无 numpy/cv2(算法用纯 Python 实现,便于零成本验证)。

    python -m pip install Pillow

## 运行演示

    python demo.py

会生成合成测试图并在 `output/` 下产出:
- `bead_grid_16.png` / `bead_grid_32.png` —— 带坐标轴的像素网格(空心圆柱视觉)
- `bom_16.csv` / `bom_32.csv` —— 色号 + 精确颗数清单
- `blueprint_16.pdf` / `blueprint_32.pdf` —— 带网格/坐标/BOM 的图纸

## 用自己的图片

    python bead_pipeline.py 你的照片.jpg --size 32 --palette palettes/fuse_beads_starter.csv --out output

参数:`--size`(16/32/50)、`--bead`(circle/square)、`--k`(KMeans 预降色簇数)、
`--min-count`(稀有色合并阈值,防"1 颗豆子")、`--de`(76 或 2000 色差算法)。

## 算法对齐全案
双边滤波降噪 -> KMeans 预降色 -> CIELAB(ΔE76/ΔE2000)匹配实体色卡 -> 稀有色合并。
属传统 CV,不引入重型模型,色彩可控、算力极低。

## 待替换 / 待验证(风险点)
- `palettes/fuse_beads_starter.csv` 是 50 色起步卡,hex 为近似值;正式色卡请替换为官方目录 CSV(同格式 id,name,hex)。
- 色卡匹配精度是核心风险:建议用已知色卡样本图做回归测试,统计 ΔE 与视觉偏差。
- PDF 为纯手工生成(无第三方依赖),布局简单;正式产品建议迁到报告库以支持多图/高清导出。
## 网页演示(Phase-1 另一半:上传 -> 出 PDF)

纯软件验证的 Web 端。依赖同上(仅 Pillow)。

    python server.py

默认监听 http://localhost:8000(可用 $env:PORT=8080; python server.py 改端口)。

浏览器打开后流程:

1. 上传图片(点击或拖拽)
2. 选尺寸(16/32/50)与豆型(圆豆/方豆)
3. 查看处理进度 -> 像素网格预览 + BOM 清单
4. 结算页(阶梯定价) -> 下载 PDF 图纸

### API
- GET / —— 上传页(index.html, CDN React + Babel,单文件)
- GET /api/config —— 返回尺寸/豆型/定价/色卡数,网页据此渲染
- POST /api/process  {image_b64,size,bead} —— 返回 {pdf_b64,grid_b64,bom:[{id,name,hex,count}]}

### 可配置项(改 server.py 无需改前端)
- SIZES —— 可选尺寸(默认 [16,32,50])
- BEADS —— 豆型列表(默认 圆豆/方豆)
- PRICING —— 阶梯定价,键为尺寸,值为 (颗数文案,价格),例 {16:('256 颗',9.9),...}
- 色板 —— palettes/*.csv,格式 id,name,hex;当前 50 色 use_beads_starter.csv

### 验证
    Invoke-WebRequest http://localhost:8000/api/config   # 应返回配置 JSON
    Invoke-WebRequest http://localhost:8000/api/process -Method Post -Body (ConvertTo-Json @{image_b64='...';size=16;bead='circle'}) -ContentType application/json  # 应返回 pdf_b64/grid_b64/bom
