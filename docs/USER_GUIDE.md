# Procedural World Studio 用户说明

## 1. 第一次运行

启动 `ProceduralWorldStudio.exe` 后，左侧是生成参数和图层操作，中央是世界画布，右侧是 Inspector 与统计信息。首次打开可以点击 **Demo / Coastal City**，载入固定 Seed 的海岸城市示例。

如果使用源码运行：

```powershell
python main.py
```

没有安装 PySide6 时，程序会尝试使用 Tkinter 兼容界面；生成器和导出器不依赖 GUI。

## 2. 生成一个世界

1. 设置 `Seed`。相同 Seed、参数和 Generator Version 会产生可复现结果。
2. 选择 Terrain：`Flat`、`Island`、`Coast`、`River` 或 `Hills`。
3. 选择道路模式：`Grid`、`Organic`、`Radial` 或 `Hybrid`。
4. 点击 **Generate**。生成过程在后台执行，状态栏会显示当前阶段，不会阻塞窗口。
5. 在画布上点击对象，右侧 Inspector 显示 `ID / Type / District / Parent / Seed / Parameters`。

## 3. 图层重生成与 Seed Lock

生成完成后可以只重生成一个层：

- `Roads`：保留地形，重新布置道路和后续受影响的地块。
- `Districts`：保留地形与道路，重新分配功能区。
- `Buildings`：保留道路、区域和地块，只替换建筑体块。
- `POI`：只重新布置车站、学校、医院、商场、公园等兴趣点。

在重生成前可锁定 `Terrain`、`Road`、`District` 或 `Building`。锁定层的随机种子会写入 Generator History；解锁后才会接受新的派生 Seed。

## 4. 港口示例

选择 `Coast` 地形并启用 Harbor Generator。港口会同时考虑海岸线、码头朝向、仓储区、工业道路、车站/铁路预留和商业区位置。港口不是把几个随机方块放在海边；Inspector 中可以查看每个 Dock、Warehouse、Station Area 的父子关系。

## 5. 编辑、撤销与恢复

点击对象后，右侧 Inspector 的 **Edit Selected** 可以对对象做可序列化编辑：输入 `Move X / Move Y` 平移几何；选中建筑时还可以修改 `Floors`，体块高度会同步更新。使用 `Ctrl+Z` 撤销、`Ctrl+Y` 恢复。生成、图层重生成、参数修改和对象移动都作为可回退操作写入历史。

## 6. 视图

使用 Camera 菜单切换：

- **Top Down**：检查道路、地块和区域边界。
- **Isometric**：观察建筑体块和高差。
- **Perspective**：检查港口、车站及街区空间关系。
- **Overview**：显示整张城市地图和统计摘要。

## 7. 导出

在 **Export** 菜单中选择目标目录和格式。建议先导出 JSON 作为可编辑主文件，再导出 GeoJSON/PNG 用于检查，最后导出 OBJ 或 glTF 给 Blender、Godot、Unreal 做后续场景制作。

JSON 会保留：

- 全部实体 ID 与父子关系
- 每个实体的类型、区域、参数和派生 Seed
- Seed Lock 状态
- Generator Version 与历史记录
- 统计信息

当前桌面界面的主要动作是导出；需要脚本化回读时，可以使用核心 API：

```python
from procedural_world_studio.core import WorldState
state = WorldState.load_json("demo/coastal_city/world.json")
print(state.seed, len(state.buildings))
```

## 8. 大型城市与性能

大型地图建议先使用 Overview 视图，再按需展开邻居。生成任务使用后台线程；空间索引、几何缓存和延迟绘制用于避免 UI 长时间卡死。如果一次生成的对象过多，可先降低地图分辨率、街道密度或建筑上限，再逐层提高。

## 9. 常见问题

**同一个 Seed 的结果不同？**  检查 Generator Version、地形/道路参数和各层锁定状态是否一致；不要在生成过程中修改参数。

**导出的 OBJ 没有材质？**  基础 OBJ 只保证几何和分组；颜色、区域与语义信息请使用同目录 JSON/GeoJSON，或选择 glTF 导出。

**窗口打不开？**  在项目根目录运行 `python main.py`，查看终端中的错误。Windows 打包版可运行 `ProceduralWorldStudio.exe --safe-mode`（如果该版本提供）以禁用高级渲染。
