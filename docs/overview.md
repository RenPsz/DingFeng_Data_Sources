# cnfin v1.1.0 生产审计与测试交付说明

## 状态

**已完成。** 原 `cnfin.py` 从“联网 happy path 能跑”升级为带数据口径校验、安全缓存、故障隔离、自动化测试和可发布制品的底层数据模块。

## 本次交付

| 交付物 | 内容 |
|---|---|
| `cnfin.py` v1.1.0 | 修复 26 项 P1；统一契约、严格输入校验、源边界异常包装、trace 内存封顶 |
| `tests/` | 85 项离线测试（单元 / 回归 / 门禁 / E2E）+ 5 项真实网络集成 |
| `.github/workflows/` | 默认 CI、每周/手动真实网络测试、tag 发布流水线 |
| `pyproject.toml` | 单模块打包、`cnfin` CLI 入口、pytest/coverage/ruff 配置 |
| `AUDIT.md` | 带行号的审计清单、修复状态、12 项剩余 P2 与分阶段路线 |
| `TESTING.md` | 测试金字塔、marker 规范、CI/CD、发布回滚、扩展源测试清单 |
| `README.md` | 更新安装、代理、数据契约、失败语义、缓存安全与质量工程说明 |
| `dist/` | `cnfin_data-1.1.0-py3-none-any.whl` + sdist |

## 关键决策

1. **宁可报错降级，不静默返回错数据**：复权缺失、分页不完整、批量部分成功、宏观审定列缺失均拒绝返回。
2. **故障分类必须准确**：只有 `SourceError` 计入熔断；`NoDataError` 表示停牌/非交易日；程序错误直接抛出不掩盖。
3. **缓存安全优先**：pickle 改为 JSON + 完整键 SHA-256 + 原子替换，消除不可信反序列化风险。
4. **实例隔离**：每个 `CNFin` 独立 HTTP Session/限速/熔断；代理只绑定代理会话，直连回退是真直连。
5. **离线测试作为默认门禁**：真实网站测试保持独立 marker，避免第三方波动阻塞 PR。

## 验证结果

- 离线：**85 passed / 5 deselected**
- 分支覆盖率：**72.88%**（门禁 70%）
- 真实网络：**5 passed**（腾讯行情/K线、东财财务/宏观、baostock）
- 静态检查：`ruff` 通过
- 构建：wheel + sdist 成功，`twine check` PASSED
- 安装：干净 venv 安装 wheel，`import cnfin`、19 项宏观目录、`cnfin catalog` 均通过

## 使用

```bash
pip install ".[dev]"
python -m pytest -m "not network"
python -m pytest -m network        # 可选
python cnfin.py health
```

## 后续建议

1. 为三大报表定义核心科目白名单（当前原始宽表列随源不同）。
2. 对财务摘要做跨源单位/口径差分测试。
3. 增加缓存自动淘汰（当前过期文件需 `clear_cache()` 清理）。
4. 生产环境补充监控指标与 trace_id 归因。

> 注意：当前目录尚未初始化 Git 仓库，工作流文件已就绪；推送到 GitHub 并配置仓库后即可触发 CI。
