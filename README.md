# 现场作业资质名单同步与异常隔离

离线采集工程使用 Python 标准库和 SQLite。adapter 读取本项目的真实来源格式，sources 提供可重复响应序列，capture 原子归档解析后的来源记录。--capture 只记录原始观察，不作业务投影；--run 在原始采集之上执行代次化同步：名册批按 generation 推进水位，任职与证书以闭区间保存，身份冲突与代次回退进入隔离，业务投影按指定日期还原在岗与资质快照。

## 环境与准备

推荐 Python 3.12.12，支持 Python 3.11+；requirements.lock 为空表示没有第三方包，运行、测试和验收均不下载数据或依赖。Dockerfile 提供同一 Linux 环境。需要容器时先在正式运行之外准备：

```sh
docker build -t q010-credential-roster:initial .
docker run --rm q010-credential-roster:initial
```

## 基线测试与原始采集

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q collector_10 tests acceptance
scratch=$(mktemp -d)
python3 -m collector_10.cli --database "$scratch/raw.sqlite3" --capture fixtures --route /roster
python3 -m collector_10.cli --database "$scratch/raw.sqlite3" --capture fixtures --route /roster
```

第一次 decoded=3、inserted=3，重抓同一观测 inserted=0。临时库不提交。tests 是已实现采集基础能力的回归测试，不代表扩展功能完成。

## 来源协议

CSV generation 是整批源版本，必须先校验完整性再推进名册水位；complete=false 表示采集批未完成，不能因缺席推断离职。person_id 为合成稳定身份，同名不同 ID 不合并，同 ID 姓名冲突隔离不改既有身份；所有名称、证号均为虚构样本，不接入真实个人信息。

任职与证书日期均为闭区间，空 end 表示仍在岗；按 on 查当日项目任职与证书是否匹配 person 及覆盖当日。certificates.json 独立采集，证书续期不能覆写过去有效期。work.json 里的工作记录按 worked_on 关联历史任职，不按 received_on 关联今天项目。generation 回退整批隔离；同代重导不重复建区间，只有完整无冲突批才能推进 generation。

FixtureTransport 每次进程创建从 routes.json 首条响应开始，同一路由多次 request 依次推进，最后一条可重复；calls 保存请求证据。源数据相对 fixture_root 读取，不能越出该目录。所有响应是本地合成数据，不发出公网请求。单场景 JSON 的 fixture_root 相对当前工作目录解析；在仓库根目录执行即可。run_id 是采集批次标识，name 仅作展示标签。

## 扩展接口与可观察输出

`python3 -m collector_10.cli --run 场景文件 --database 状态库 --output 结果文件`

场景文件含 name、run_id、sources（有序根来源）、options、fixture_root；name 只作日志标签。expected 是公开最小预期，产品不应读取它。所有来源和选项应独立支持；四个场景不能替代全部边界测试。业务结果投影：roster（person、project、qualified）、generation、work（id、linked、project）、quarantine、intervals（含来源代次与任职、证书区间）。数组顺序不影响检查，公开预期数组是子集，但空数组要求确实为空。内部 SQLite 表、架构和调度实现由开发者选择。每个场景在同一状态库上用两个独立进程执行，以检查重启和重复输入；业务投影应保持一致，notifications 为数量时表示此次新建通知数，第二次应为0，为数组时表示持久化待处理通知清单。

例如：`python3 -m collector_10.cli --run fixtures/past_roster.json --database collector_10.sqlite3 --output past_roster.json`（在仓库根目录执行，生成的状态库与结果文件不提交）。

## 同步语义

- 批次应用是事务性的：任一来源完整性校验失败，本次运行整体回滚，水位与区间不变。
- 单批 CSV 只允许一个 generation；低于当前水位的批次整批隔离（generation_rollback），不建区间、不回退水位。
- 同代重导按区间自然键幂等，不重复建区间；只有完整无冲突批才把水位推进到更高 generation。
- 身份按 person_id 登记首个姓名；同 ID 新姓名进入隔离（identity_conflict），冲突行不落区间、既有身份不变，本批不再推进水位；同名不同 ID 各自独立，不合并。
- 任职区间空 end 表示仍在岗；后续批次显式给出同一区间（同人同项目同 start）的 end 时，视为同一任职段的离职信息并闭合该区间，closed_generation 记录来源代次。不从缺席推断离职：complete=false 禁止该推断，complete=true 时本实现也不自动闭区间，离职以源区间 end 为准，历史区间永不删除。
- certificates.json、work.json 作为 fixture_root 下的独立伴随来源，每次运行幂等并入；证书续期只追加新区间，不覆写过去有效期，资质按当日覆盖的任一区间判定。
- work 记录按 worked_on 匹配当日任职区间判定 linked 与归属项目，与 received_on 无关；离职后补传的记录仍归属原项目。
- quarantine 持久化并按（reason、source、generation、person）去重；notifications 输出本次运行新建的隔离条数，重放为 0。

## 验收与正式展示

```sh
python3 acceptance/check.py --contracts-only
python3 acceptance/check.py
```

contracts-only 仅检查材料结构，不能当成功能验收。第二条执行四个场景的功能验收，应全部 PASS；每个场景独立临时目录，执行两次、每次进程超时5秒，八次总预算40秒，无固定 sleep。正式展示从干净状态执行基线测试和第二条命令，预算小于90秒；依赖和镜像预先准备，不能用旧日志代替运行。边界回归见 tests/test_run.py（同名人员、证书边界日、回滚后再同步、续期、迟到工作记录等），不修改预期来迁就实现。
