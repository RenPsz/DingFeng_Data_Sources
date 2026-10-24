# cnfin 测试策略与 CI/CD 发布流水线

本文定义 `cnfin.py` 作为**底层数据获取模块**的质量工程标准：测试分层、隔离原则、门禁、发布与回滚。

---

## 一、测试目标

1. **可重复**：默认测试不依赖外网、不依赖当天行情、不受第三方网站波动影响。
2. **可解释**：数据口径（复权/频率/单位/区间/报告期）错误必须被测试捕获，而不是只测“有没有返回”。
3. **可控**：真实网络测试显式标记、独立运行，不阻塞日常开发。
4. **可发布**：每次发布都经过同一套构建、安装、版本一致性验证。

---

## 二、测试金字塔

| 层 | 文件 | 依赖 | 证明什么 |
|---|---|---|---|
| 单元测试 | `tests/test_symbols.py`、`test_http_cache.py`、`test_adapters.py`、`test_router_financial_macro.py` | mock HTTP / mock 外部库 | 解析、单位换算、缓存安全、熔断语义、降级、输入校验 |
| 回归测试 | `tests/test_regressions.py` | mock | 每个已审计缺陷都有专属失败用例，防止复发 |
| 集成测试 | `tests/test_kline_facade.py` | mock 路由边界 | 门面契约：排序、过滤、limit 取最新、批量完整性、schema |
| E2E（离线） | `tests/test_e2e.py` | 子进程 + 真实 CLI | 安装形态、CLI 退出码、公开工作流 |
| 集成测试（网络） | `tests/test_integration_network.py` | 真实 Internet | 真实源当前可用性与数据契约（唯一性、单调性、OHLC 关系） |

### marker 约定（强制）

| marker | 含义 | 默认 CI |
|---|---|---|
| （无） | 离线单元/回归 | ✅ 运行 |
| `e2e` | 离线端到端（子进程/CLI） | ✅ 运行 |
| `integration` | 涉及外部服务或可选依赖 | ❌ 不在默认门禁 |
| `network` | 真实访问 Internet | ❌ 只在 `network.yml` 运行 |

`pyproject.toml` 已开启 `--strict-markers --strict-config`，拼写错误的 marker 会让收集阶段直接失败。

---

## 三、离线测试设计要点

1. **mock 的是传输层，不是被测逻辑**：适配器解析、单位换算、路由降级、缓存读写全部走真实代码，只把 `requests` 会话替换成受控响应。
2. **先失败后修复**：每个审计缺陷先写一个能复现旧 bug 的测试（如“hfq 缺失不得返回 raw”），确认在旧代码上失败，再修复并转绿。
3. **不硬编码行情数值**：所有断言针对结构与不变量（日期唯一、升序、`high>=low`、`volume>=0`、市场字段正确），避免因真实行情变化导致 flaky。
4. **缓存测试使用临时目录**：`tmp_path` 隔离，测试并发写入、损坏文件、TTL 过期、键碰撞。
5. **HTTP 测试注入假 Session**：分别模拟代理/直连，验证回退顺序、403 计数、熔断冷却、单次连接失败不误熔断。

---

## 四、真实网络测试契约

运行方式：

```bash
python -m pytest -m network
```

覆盖（见 `tests/test_integration_network.py`）：

- 腾讯实时行情：code/market/price 非空；
- 腾讯 K 线：列契约、日期唯一且升序、`high>=low`、`volume>=0`、limit 语义；
- 东财财务摘要：report_date 非空、report_type 属于四类报告；
- 东财宏观 tidy：审定数值列、indicator 标识；
- baostock（未安装自动 skip）：不复权日线与最新 limit。

**刻意不做的断言**：不比较“今天的最新价”与某个写死的数字；不做依赖盘中状态的精确涨跌幅断言。金融数据测试的稳定性来自契约断言，不是快照数值。

---

## 五、CI 流水线（`.github/workflows/ci.yml`）

```
offline-test (matrix)
  ├─ Python 3.10/3.11/3.12/3.13 @ ubuntu
  ├─ Python 3.13 @ windows（覆盖 Windows 原子替换/路径行为）
  ├─ ruff check cnfin.py tests
  ├─ pytest -m "not network" --cov-fail-under=70
  └─ 上传 junit.xml / coverage.xml
build
  ├─ python -m build
  ├─ twine check dist/*
  ├─ 干净 venv 安装 wheel
  ├─ import cnfin + macro_catalog + cnfin catalog
  └─ 上传 dist 制品
```

设计原则：

- **第三方网站波动不能阻塞 PR**：network 测试不在默认 CI。
- **覆盖率是门禁不是装饰**：分支覆盖率低于 70% 直接失败。
- **Windows 至少一个 job**：本项目目标环境包含 Windows，原子缓存替换与路径行为必须实测。
- **每个 job 有 timeout**，防止卡死占用 runner。

---

## 六、发布流水线（`.github/workflows/release.yml`）

触发：推送 `v*` tag。

```
verify-build-release
  ├─ ruff check
  ├─ pytest -m "not network" --cov-fail-under=70
  ├─ python -m build
  ├─ twine check dist/*
  ├─ tag 与 cnfin.__version__ 一致性校验
  ├─ 干净 venv 安装 wheel 冒烟
  └─ GitHub Release 附 wheel / sdist / cnfin.py
```

发布规则：

1. **版本来源唯一**：`pyproject.toml` 动态读取 `cnfin.__version__`，tag 必须等于 `v{__version__}`。
2. **发布前重跑门禁**：不信任“上次 CI 是绿的”，tag 提交上重新验证。
3. **制品三件套**：wheel、sdist、单文件 `cnfin.py`（满足“拷走即用”的原始需求）。
4. **回滚**：单文件场景直接回退到上一个 Release 的 `cnfin.py`；包安装场景 `pip install cnfin-data==<上一版本>`。缓存为 JSON 向后兼容，无需清理。

---

## 七、本地工作流

```bash
# 一次性环境
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"

# 日常（提交前）
.venv\Scripts\python -m ruff check cnfin.py tests
.venv\Scripts\python -m pytest -m "not network"

# 覆盖率明细
.venv\Scripts\python -m pytest -m "not network" --cov=cnfin --cov-branch --cov-report=term-missing

# 真实网络（可选）
.venv\Scripts\python -m pytest -m network

# 构建与安装冒烟
.venv\Scripts\python -m build
.venv\Scripts\python -m pip install --force-reinstall dist\cnfin_data-*.whl
.venv\Scripts\cnfin catalog
```

---

## 八、新增数据源的测试清单

给新 `BaseSource` 子类提交时，至少补：

1. **quote**：市场字段、单位换算、部分返回必须抛错；
2. **kline**：复权 key 精确匹配、频率映射、成交量单位、limit 取最新、日期过滤；
3. **financial/macro**：报告期推导、审定数值列、schema 校验；
4. **故障**：403/连接错误/空数据分别走 `SourceBlocked`/`SourceError`/`NoDataError`；
5. **门面**：`prefer` 新源缓存键隔离、降级链位置；
6. 若该源需要真实网络验证，加 `@pytest.mark.network`，不要放进默认 CI。

---

## 九、质量红线

- 任何“静默返回其他口径/部分数据”的行为都是缺陷，不允许以“上游就这样”为由放过。
- 任何新修复必须先有一个能复现旧问题的回归测试。
- 默认 CI 必须离线可重复；需要真实网络的能力必须显式 marker。
- 覆盖率下降、ruff 失败、twine check 失败、版本不一致，任一发生即禁止发布。
