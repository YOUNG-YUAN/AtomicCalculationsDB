# FixTask_07_08.md — 设置页交互细节

## 1. 选中态改为勾选标记

`app_core/ui/theme/m3_theme.qss` 原来只有：

```css
QCheckBox::indicator:checked {
    background-color: #0B57D0;
    border-color: #0B57D0;
}
```

选中后只是一个蓝色实心块。现在新增白色勾形资源：

```text
app_core/ui/assets/checkmark.svg
```

`load_stylesheet()` 追加：

```css
QCheckBox::indicator:checked {
    image: url(".../assets/checkmark.svg");
}
```

保留蓝色选中底色，并在中间绘制白色勾；未选中仍是白底描边。审计导出、MinerU 开关等全部 QCheckBox 共用该样式。

## 2. 禁止未点开时滚轮修改下拉框

新增 `app_core/ui/controls.py`：

- `WheelSafeComboBox`
- `WheelSafeSpinBox`
- `WheelSafeDoubleSpinBox`

行为：

- 下拉框关闭时，滚轮事件 `ignore()`，交给外层滚动区域滚动页面；
- 下拉框弹层打开时，滚轮仍交给 Qt 原生逻辑切换选项；
- 数字框没有键盘焦点时忽略滚轮，避免鼠标经过就改值；
- 有焦点时保留原生滚轮步进。

所有 UI 模块的 `QComboBox`、`QSpinBox`、`QDoubleSpinBox` 已统一替换为这些子类，包括：

- 设置页 / MinerU；
- 模型池与策略编辑；
- 审计导入；
- 探索中心、文献库、工作台、同步与 MCP。

## 3. MinerU 字段宽度

MinerU 卡片中的短内容字段改为左对齐并限制最大宽度：

| 字段 | 最大宽度 |
|---|---:|
| 解析档位 | 220 px |
| OCR 模式 | 220 px |
| 设备 | 180 px |
| 超时（秒） | 140 px |

命令 / 可执行文件仍占满可用宽度，因为它可能包含完整路径。

## 4. 数字框上下按钮

`QSpinBox` / `QDoubleSpinBox` 新增：

- 右侧 24 px 按钮区；
- 上下箭头分别使用 `chevron-up.svg` / `chevron-down.svg`；
- hover 时浅灰背景；
- `padding-right: 30px`，数字不再压到按钮上。

原先截图里像“黑色括号”的默认箭头现在是清晰的灰色上下折线。

## 5. 测试

新增 `tests/test_ui_controls.py`：

- 样式表包含勾选资源与上下箭头资源；
- 关闭状态下的下拉框收到滚轮不改变当前项；
- 无焦点的数字框收到滚轮不改变数值。

`tests/test_gui_v2.py` 增加：

- MinerU 四个字段使用 wheel-safe 控件；
- 四个字段的最大宽度符合预期。

全量回归：**160 passed**。

## 6. 发布状态

已升级到 **2.5.2** 并生成：

- `release/AtomicCalculationDB_Patch_v2.5.2.zip`（452.9 KiB，94 个文件，SHA256 `19c693806d1f1d0edad71592ee82a0abcf1f2ec27fea0fdd0ea2126a5c4dea8d`）
- `release/AtomicCalculationDB_Full_v2.5.2.zip`（1,635.1 MB，9,959 个文件，SHA256 `4f58ec65a22e61110bdfe52407dc48888c07ef5a9c8cba61c32bf2842e0cfbe3`）

两个包均通过 `unzip -t`。安装目录已热更新到 2.5.2，旧 `app_core` 位于 `app_core_bak`（2.5.1）。
