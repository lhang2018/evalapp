# 发现与决策

- 当前API仅静态operator/admin角色令牌，本卡把这两个凭证类别及本地宿主绑定为明确服务主体；不宣称增加完整多用户登录/RBAC。
- 服务端配置白名单主体可选择的user/project/session三元组；task由服务端生成，expert由实际专家/蜂巢键决定。客户端只能提交匹配白名单的memory_context和已有grant_ref，不接受自填actor/grant对象。
- 身份以独立不可变绑定表为权威，Task只存ref和快照；恢复校验而不退化为无身份读取。历史无绑定任务自动进入任务专属legacy主体，旧全局私有记忆不自动授予。
- 现MemoryStore仅按单层scope_key隔离，两个用户/项目/会话共用专家键会串读；重复内容/事实冲突也仅按scope/key判断。为真正消费身份，需最小补齐owner链，grant按目标完整身份且仅扩展授权scope。
- ContextManifest已经生成但Controller丢弃；压缩摘要元数据还保留约束/验收正文。需引用化并持久化task/run关联，不改已冻结RuntimeManifest。
- 新表走schema域模块，不修改历史迁移；Task JSON新增兼容字段无需ALTER现有表。
- 探查阶段PowerShell下rg位置通配符不展开、checkpoint文件名猜测错误；已改为rg -g及读取实际controller相关段，不把检索错误当产品问题。
