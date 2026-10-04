# 固定样地复测森林生长 / 死亡 / 进界量评估系统

林业研究站比较固定样地两次调查（本演示为 **2019 → 2024**）的：

* **存活木生长量**（survivor growth）
* **死亡量**（mortality）
* **进界量**（ingrowth，胸径 ≥ 5 cm 阈值）

净变化恒等式：

```
Δ生物量 = 存活木生长 − 死亡 + 进界
```

技术栈：**React（Vite）+ Django REST Framework + NumPy/SciPy + PostgreSQL/PostGIS**
（开发环境用 sqlite3 也能完整运行；PostGIS 层见 `deploy/postgis.sql`）。

> 数据均为**虚构**演示数据（树种、方程、坐标、样地），仅用于说明流程。

---

## 1. 关键规则（对应验收要求）

### 1.1 胸径单位必须显式记录
* 每条测量必须带 `dbh_unit`（`cm`/`mm`/`in`），树高单位 `m`。
* 入库时转换为规范单位（胸径 cm、树高 m），**原始值与单位同时保留**，可审计。
* 合理范围检查拦截单位错误（如把 250 mm 当成 250 cm、树高 950 m）。
* 树干坐标必须落在样地边界内（PostGIS 层有 `ST_Contains` 约束兜底）。

### 1.2 异速生长方程显式记录适用树种
`AGB_kg = a · dbh_cm^b · h_m^c`，方程记录：
* 适用树种列表（多对多）、胸径适用范围、是否需要树高；
* 系数 a/b/c、残差 σ(ln AGB)、文献引用、版本号；
* 超出适用径阶的树会在结果中标记 **extrapolation**。

### 1.3 编号是标签，不是身份
**编号相同但位置矛盾时，先核实，不能直接认成同株。**

* 内部 `tree_id` 才是个体身份；同树行换标签 = 已核实改号（renumber）。
* 同编号、不同树行、位置矛盾 → 生成 `IdentityConflict`（open），
  在人工核实前**从所有分量中剔除**，不会悄悄变成死亡或进界。
* 核实结论只有人工给出：
  * `renumber`：同一株树换了牌号 → 计入存活木生长；
  * `distinct`：不同个体 → t1 计入死亡、t2 计入进界。
* 新编号出现在旧树附近 → “possible renumber” 待核实，绝不自动合并。

### 1.4 真实零生长、缺测、死亡三者严格区分
| 情况 | 字段 | 处理 |
|---|---|---|
| 真实零生长 | `alive_measured`，两次均测，|Δdbh| ≤ 0.15 cm 且有复核记录 | 计入生长量（增量≈0），结果中列出 |
| 缺测 | `alive_not_measured`（活着但胸径未测） | **不是零**；按样地比率插补，方差膨胀，列出清单 |
| 死亡 | `dead`（t2 有死亡观测） | 以 t1 生物量计入死亡量 |
| 未找到 | `missing_tree` | 不入死亡量，列入 provenance |
| 进界以下 | t2 新树 dbh < 5 cm | 记录但不计入进界 |

### 1.5 总体估计按抽样设计加权
分层简单随机抽样，**不把所有树木平均后乘面积**：

```
样地分量 y [kg/ha] = 分量(kg) / 该样地自己的面积(ha)
Y_h = A_h · mean_h(y)                 # A_h = 已知的层土地面积
SE_h = A_h · sqrt( (1−f_h) · s_h²/n )  # 有限总体校正可选
Y = Σ_h Y_h，SE 跨层合成（Welch–Satterthwaite 自由度，t 分布 95% CI）
```

演示数据刻意使用**不等面积样地**（0.20 / 0.50 / 1.00 ha）。

### 1.6 已确认调查版不可被新方程静默改变
* `EstimateVersion`：draft → `confirm` 后结果载荷、设计快照、方程校验和全部冻结。
* 模型层 + PostGIS 触发器双重禁止修改 confirmed 版本。
* 确认时同时**锁定所用方程**（系数不可改）；新系数必须以**新方程 code/version** 录入，
  并产生**新版本估计**，旧版本数字永不改变。

### 1.7 测量更正单（MeasurementCorrection）闭环
外业复核发现转录错误（如 **2024 复测把 25.0 cm 误抄为 250（mm）**，旧批量导入
把规范列错存成 250 cm，且错误值已进入已确认估计）时，走更正单闭环而**绝不改历史行**：

* **更正单必须指向原 `TreeMeasurement`**，提交时快照：
  原始 raw 值与单位、规范值、坐标 `(x_m,y_m)`、状态、原因、证据清单（照片/内业页/手簿）。
* 状态流转：`pending → reviewed → applied | rejected`；
  `failed` 是应用未完成时可重试的侧态（中断、或被 open 身份矛盾拦截）。
* **幂等提交**：同一 `idempotency_key` 永远返回同一张更正单（第二次 `200` +
  `Idempotent-Replay: true`），不会生成第二张。
* 应用时**新增一条可追溯的 `MeasurementRevision`**（原始测量行永不被覆盖），
  修订链 `supersedes` 可回看；每条测量至多一条 `effective` 修订（部分唯一索引兜底）。
* 应用后**重新扫描身份矛盾**（基于生效坐标，而非旧基础行）。
  坐标更正若使该株落入某个 **open** `IdentityConflict`，应用返回 **409**、
  更正单置 `failed` 并把新触发的矛盾打标签落库——**不得绕过人工核实**；
  人工 resolve（renumber/distinct）后重试才可应用。
* **只有新的 draft `EstimateVersion` 会使用修订**（M2M + design snapshot +
  provenance 三重记录修订 ID）；已确认版本的结果载荷、方程校验和、来源
  （provenance）原样可回看，数字逐字节不变。
* 两阶段落库（`pending → effective`，中断残留标 `void`）+ 启动/读取时
  `reconcile_interrupted_applications()`：应用中断后刷新只看到完整的
  pending/failed 状态；重试幂等，**不会产生第二个有效修订**。
* API：提交 / 审核 / 影响范围（新旧差异、被阻断身份项、确认版清单）/ 应用 /
  按更正单重算（生成新 draft），外加只读修订台账 `/api/revisions/`。

---

## 2. 不确定性假设（结果中完整输出）
1. **设计推断**：分层 SRS，每样地等权（每公顷基准），层土地面积放大；树木从不合并平均。
2. **测量误差**：胸径 σ=0.10 cm、树高 σ=0.30 m，独立高斯，一阶误差传播；
   作为诊断分量单独报告（不与样地间抽样方差重复计入 SE 合计）。
3. **方程残差**：乘法对数正态 σ(ln AGB)；存活木两次用同一方程、残差假定完全相关故增量抵消；
   死亡（仅 t1）与进界（仅 t2）的方程误差保留。
4. **缺测存活木**：假定样地内 MAR，用“有测样木 t1 生物量生长率”做比率插补，
   抽样方差按 1/(1−缺测生物量比例) 膨胀。
5. 未核实身份、未找到、进界以下个体**排除在分量外**并在 provenance 列明。
6. 95% CI 用跨层 Welch–Satterthwaite df 的 t 分布；净变化 SE 中分量抽样协方差假定为 0。
7. 样地申报面积与多边形面积交叉核对（1% 容差）。

---

## 3. 运行

### 后端
```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate      # 可选
pip install -r requirements.txt
python3 manage.py migrate
python3 manage.py seed_demo        # 载入虚构数据（含全部验收场景）
python3 manage.py runserver 127.0.0.1:8123
```

PostgreSQL/PostGIS：
```bash
FOREST_DB=postgis PGHOST=.. PGUSER=.. PGPASSWORD=.. \
  python3 manage.py migrate
psql -d foreststation -f ../deploy/postgis.sql
```

### 前端
```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, /api 代理到 8123
```

界面四页：
1. **Plots & individuals**：SVG 地图显示全部样地边界与 t2 个体状态；点入样地看 t1→t2 复测、
   改号、零生长/缺测/死亡着色；测量值以**生效视图**展示（修订覆盖），基础历史值划线保留，
   修订链与更正单号内联显示；
2. **Identity conflicts**：编号矛盾核实工作台（renumber / distinct），
   含“由哪张更正单触发”的标记；
3. **Measurement corrections**：测量更正单工作台——提交链/审核/影响范围
   （新旧差异、被阻断身份项、确认版永不改变）/ 应用 / 按更正单重算新 draft；
4. **Estimates**：选择方程→跑 draft→查看分量、来源、不确定性、**修订链**与被阻断身份项
   →确认冻结。

---

## 4. API 摘要
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/plots/` | 样地位置、边界、面积、CRS |
| GET | `/api/measurements/?campaign=2024` | 每株每期测量（含原始/规范单位） |
| GET | `/api/equations/` | 方程、系数、适用树种、径阶范围 |
| GET | `/api/conflicts/?status=open` | 同号位置矛盾 |
| POST | `/api/conflicts/{id}/resolve/` | `{status: renumber|distinct, note}` |
| POST | `/api/imports/` | 批量入库（拒收单位错误/越界行，207 返回明细） |
| POST | `/api/estimates/` | 运行 draft 估计（基于当前生效测量值） |
| POST | `/api/estimates/{id}/confirm/` | 冻结版本并锁定方程 |
| GET | `/api/estimates/{id}/` | 完整结果：分量 + 来源 + 不确定性 |
| GET/POST | `/api/corrections/` | 更正单列表 / 幂等提交（pending） |
| POST | `/api/corrections/{id}/review/` | 审核：`{decision: apply|reject, …}` |
| GET | `/api/corrections/{id}/impact/` | 影响范围：新旧差异、身份阻断项、确认版清单 |
| POST | `/api/corrections/{id}/apply/` | 应用：追加修订 + 重扫身份（可重试，409=被矛盾拦截） |
| POST | `/api/corrections/{id}/recompute/` | 按该修订重算，生成**新 draft** |
| GET | `/api/revisions/` | 修订台账（effective/superseded/void 全保留） |

### 入库行示例
```json
{
  "campaign": "2024",
  "rows": [{
    "plot": "P01", "field_number": "001", "species": "OAK",
    "x_m": 500010.0, "y_m": 4000010.0,
    "status": "alive_measured",
    "dbh_raw": 252, "dbh_unit": "mm",
    "height_raw": 16.8, "height_unit": "m"
  }]
}
```

---

## 5. 验收测试
```bash
cd backend && python3 manage.py test inventory
```
20 个测试覆盖：改号、同号位置矛盾（剔除→核实 distinct 后才入死亡/进界）、
不等面积按样地扩展、单位错误拒收、零生长/缺测/死亡区分、已确认版本对新方程与直接篡改免疫，
以及测量更正单闭环（G 组 8 项）：

* **G1 单位更正**：审核通过并应用后，**新 draft 数字改变、旧 confirmed 逐字节不变**
  （结果、方程校验和、provenance 均可回看），仅新 draft 记录修订 ID；
* **G2 幂等**：相同幂等键重复提交只得到**同一张**更正单（200 + 重放头）；
* **G3 矛盾审核**：同一测量已有未结更正单时禁止再开；rejected 的不能被 apply；
  互斥结论不可能同时 applied；
* **G4 坐标更正人工门**：坐标更正触发 open `IdentityConflict` 时应用 409/failed、
  新矛盾打标签落库，人工 resolve 后重试才生效——不得绕过人工核实；
* **G5 中断恢复**：应用中断（pending 修订残留）刷新后只保留完整 void/failed 状态，
  重试幂等、**不会生成第二个有效修订**；
* **G6/G7 修订台账、provenance 追踪、链式更正 supersedes**：基础行永不被覆盖。

## 6. 虚构演示数据场景索引
* `P01/004` 两次胸径相同 → **真实零生长**；
* `P01/005` 活着未测胸径 → **缺测比率插补**；`P02/003`、`P04/006` 同；
* `P01/006`、`P03/004`、`P04/005` → **死亡**；`P02/005`、`P05/006` → **未找到**；
* `P01/007→017` → **已核实改号**（同一 tree 行）；
* `P01/008`、`P01/009` → **同号位置矛盾，open 剔除**；`P02/117→118` 疑似改号 open；
* `201` 系列（dbh 4.2–6.4）→ 进界阈值边界，<5 cm 排除；
* `P04/002` dbh 102 cm → **超出方程径阶范围**标记；
* 4 条坏行（mm 当 cm、树高 cm 当 m、缺单位、坐标越界）→ **入库拒收**；
* 样地面积 0.20 / 0.50 / 1.00 ha 不等。
* `P02/007` 2024 复测：真 25.0 cm 被旧批量导入误存为规范 250 cm（“25.0 cm 误抄为
  250 mm”场景），种子数据预置一张 **pending 测量更正单**
  （幂等键 `seed-P02-007-2024-unit-fix`），可在 Measurement corrections 页走
  审核→应用→重算新 draft 全流程。
