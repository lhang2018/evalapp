# 核对结论

- CXR-01：asset_impact 缺失 store 时返回 unavailable，但 errors 为空，回归门禁只消费 errors。
- CXR-02：ReleaseGate 仅在明细非空时比较数量，且基础报告校验未覆盖 holdout 两份报告。
- CXR-03：失败夹具证据遗漏 cost_known 时，沿用普通旧执行器的 cost 存在即已知兼容逻辑。
- 原 ALN-10-Q/R 已由 claude-main 独立接受；新补丁尚未独立复核，不能继承旧验收结论。
- 工作区存在 Android 通知、tests/test_e12_e13.py 和 .workbuddy 在途内容。认领表 Markdown 的现有修改只是已提交 JSON 的重新渲染，须保留已接受状态。
