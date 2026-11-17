# 运行手册 ledgerbank

## 启动与停止

```bash
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:18206/api/health
docker compose down -v --remove-orphans
```

## 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

命名卷 `ledgerbank-data` 保存批次、流水行、候选与匹配。重建后从「载入样例」重新开始。

## 主流程

1. `POST /api/demo/run`：读取两份脏样例，采信、质量、候选、匹配、入账凭证一次跑完。
2. `GET /api/quarantine`：查看隔离行与原因分布。
3. `GET /api/candidates`：查看已生成候选。
4. `GET /api/matches`：查看匹配组与手续费分摊。
5. `GET /api/postings`：查看每张匹配组的记账凭证、科目分录与来源追溯。
6. `GET /api/differences`：查看两侧守恒与差异归因。
7. `GET /api/export/report.csv`、`/api/export/postings.csv` 与 `/api/export/manifest.json`：导出。

## 入账与中断恢复

```bash
# 正式入账（同 run_id 重跑先删后插，凭证号与分录逐行一致，不会多出分录）
curl -s -X POST http://127.0.0.1:18206/api/postings
# 中断或旧版方案恢复：按当前完整方案确定性重建，不新增、不丢失分录
curl -s -X POST http://127.0.0.1:18206/api/postings/recover
```

- 入账与采信/匹配在同一 SQLite 事务提交；写入中途失败整体回滚，旧版完整凭证保留，不会出现半成品分录。
- `/api/postings` 返回 `stale=true` 表示库内是旧版方案（版本号见 `stored_scheme_version`），调 recover 即重建。
- 页面 `/postings` 与导出 `postings.csv` 读同一张 `journal_lines` 表；运行清单 `manifest.json` 的 `posting` 段带方案版本与凭证/分录计数。

## 单步重跑

```bash
curl -s -X POST http://127.0.0.1:18206/api/candidates
curl -s -X POST http://127.0.0.1:18206/api/matches
curl -s -X POST http://127.0.0.1:18206/api/postings
```

同一输入重跑命中同一 `run_id`，先删后插，匹配组、凭证号与分录行数不变。
重跑 `/api/matches` 会在同一事务内重建入账凭证，避免凭证指向已失效的匹配组。

## 导入自有文件

```bash
curl -s -X POST http://127.0.0.1:18206/api/statements/import \
  -F side=bank -F file=@sample_data/bank_statement_dirty.csv
```

支持 UTF-8（含 BOM）、UTF-8 无 BOM、GB18030；列名按 `GET /api/contract` 的别名表映射。

## 失败处理

- 400：参数或文件不可用，页面顶部提示原因，保留已填内容。
- 404：批次或样例不存在，先执行 `POST /api/demo/run`。
- 409：还没有候选就求解匹配，先执行 `POST /api/candidates`。
- 隔离行不是失败：按原因码修正源文件后重新导入。

## 日志

```bash
docker compose logs -f app
```
