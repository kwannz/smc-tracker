# 谐波页（/harmonic?v=2）排版重设计 — 设计文档

- 日期：2026-06-24
- 范围：纯前端，仅改 `src/smc_tracker/dashboard.py` 内 `_HARMONIC_DETAIL_TEMPLATE`
  及其 JS 渲染函数（`renderSvgCandles` / `renderDetail` / 5s 刷新逻辑）。
  **不动数据层/算法层/后端 API**。
- 约束：本地实现 + 截图验证；**部署须用户批准**（见 memory `harmonic-redesign-state`）。

## 目标（用户确认的痛点）
1. 图表太小 / 留白浪费（SVG 信封式 letterboxing）。
2. 要滚动才能看全（核心信息被挤到下方）。
3. 信息层级不清（核心与辅助视觉权重相同）。
4. 整体视觉陈旧 + **改成浅色/白底主题**。
5. **缺少数据实时**：右详情面板选定后是静态快照，不自动刷新。

## 设计

### ① 布局：左列表 + 「大图表 + 右信息栏」
右详情区由「单列卡片堆叠」改为：
- **主区（flex）**：大蜡烛图（核心焦点）。
- **信息侧栏（固定 ~340px）**：⚡Setup 明细（交易计划）+ 📐多周期 S/R。
- **下方全宽**：🕐历史形态 + 📖名词解释 + ⚠️disclaimer（下沉）。
- 响应式：窄屏（<1100px）侧栏堆叠到主区下方。

### ② 图表撑满修复
放弃写死 `viewBox 800×280` 导致的居中留白。渲染后用 JS 读取
`#chart-host` 实际像素宽 → `W=clientWidth`、`H≈440`，蜡烛宽/间距按真实 `n` 重算；
`preserveAspectRatio="none"` + width:100% 保证 1:1 铺满。`window.resize` 防抖重绘自适应。

### ③ 浅色/白底主题（GitHub Light 映射）
保留 CSS 变量名（含 `--bg`，测试兼容）。
`--bg:#f6f8fa  --card:#fff  --border:#d0d7de  --text:#1f2328  --muted:#656d76`
`--green:#1a7f37 --red:#cf222e --blue:#0969da --yellow:#9a6700 --purple:#8250df --orange:#bc4c00`
选中/hover：`#ddf4ff` / `rgba(0,0,0,.04)`。SVG 内硬编码色同步换成白底可读版
（蜡烛 `#26a269`/`#e5484d`，网格 `#d8dee4`，PRZ `#0969da` op .12 等）。蜡烛绿涨红跌、
S/R 红压力绿支撑、PRZ 蓝区带语义全部保留。

### ④ 视觉精致度
卡片加轻阴影 `--shadow`；Setup 卡片加强调（标题色条）；统一间距/圆角；保留等宽字体。

### ⑤ 详情面板实时刷新（核心新增）
现有 `setInterval(...,5000)` 仅刷新左列表。新增：同 5s 周期对当前 `_selectedCoin`+
`_selectedTf` 重拉 `/api/harmonic/coin/...` 并**就地重绘**，保留滚动位置 + explainer 展开态、
避免闪烁。币头部加 **● LIVE 脉冲点 + 现价（最新蜡烛收盘）+ 更新时间**。复用现有 API，不动后端，失败静默降级。

## 验证
- `./.venv/bin/python -m pytest tests/test_dashboard.py -q` 全绿（更新 dark_theme 测试 docstring，
  断言 `--bg` 仍成立）。
- 重启本地 dashboard（8788）截图核对：白底、大图撑满、侧栏布局、LIVE 实时刷新。
- `python -m py_compile` 通过。

## 不做（YAGNI）
- 不做 WebSocket/SSE 推送（用户选 5s 轮询）。
- 不改后端 API / 数据结构 / 算法。
- 不部署（待用户批准）。
