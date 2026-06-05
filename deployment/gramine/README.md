# Gramine Direct

在项目根目录执行：

```bash
export DASHSCOPE_API_KEY="your_dashscope_api_key"

gramine-manifest \
  -Dproject_dir=$(pwd) \
  deployment/gramine/vault.manifest.template \
  deployment/gramine/vault.manifest

gramine-direct deployment/gramine/vault
```

生成的 `vault.manifest` 可能包含展开后的环境变量，因此已加入
`.gitignore`，不应提交或公开。

当前 manifest 将项目挂载到 `/project`，Python 包根目录为
`/project/src`，Vault 入口为 `/project/src/trusted/vault_server.py`。
