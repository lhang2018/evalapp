import {test} from "node:test";
import assert from "node:assert/strict";
import {classifyPipeline,resultView} from "../../src/features/pipeline/chain.ts";
import {createProposal,proposalToAction} from "../../src/device/taskProtocol.ts";
import type {ActionReceipt} from "../../src/device/taskProtocol.ts";
import {NativeDeviceTransport} from "../../src/device/nativeTransport.ts";

function receipt(state:ActionReceipt["state"],terminal:boolean):ActionReceipt {
  return {effect_id:"fx",action_id:"a",proposal_id:"p",task_id:"t",run_id:"r",
    kind:"launch_intent",risk:"low",state,terminal,replayed:false,occurred_at:1,recorded_at:2};
}

test("settings proposal reaches supported native operation through real adapter",async()=>{
  const decision=classifyPipeline({text:"帮我打开设置",modeOverride:null,task_id:"t",run_id:"r"});
  assert.ok(decision.proposalInput);
  const proposal=createProposal(decision.proposalInput);
  let launched="";
  const native=new NativeDeviceTransport({isServiceEnabled:async()=>true,
    launchIntent:async target=>{launched=target;return {ok:true};},performClick:async()=>{throw Error("unexpected click");}});
  const result=await native.dispatch(proposalToAction(proposal));
  assert.equal(result.status,"verified");assert.equal(launched,"com.android.settings");
});

for (const text of ["帮我整理报告","打开设置然后发送消息","设置一个提醒"]) {
  test("work requiring planning must not invent click: "+text,()=>{
    const decision=classifyPipeline({text,modeOverride:"work",task_id:"t"});
    assert.equal(decision.proposalInput,null);
  });
}

test("device proposal without task identity stays pending",()=>{
  assert.equal(classifyPipeline({text:"打开设置",modeOverride:"work"}).proposalInput,null);
});

test("verified action is not presented as completed whole task",()=>{
  const view=resultView(receipt("verified",true));
  assert.notEqual(view.label,"已完成");assert.match(view.label,/设备|动作|操作/);
});

test("non-success receipt labels retain denial and pending state",()=>{
  assert.match(resultView(receipt("denied",true)).label,/拒绝/);
  assert.match(resultView(receipt("unavailable",true)).label,/不可用/);
  assert.match(resultView(receipt("dispatched",false)).label,/核验|确认/);
});

test("receipt cannot borrow another task identity",()=>{
  assert.throws(()=>resultView(receipt("verified",true),{local_task_id:"other",server_status:"SUCCEEDED"} as any),/身份|任务/);
});

test("inconsistent terminal flag cannot hide uncertain effect",()=>{
  const view=resultView(receipt("unverified",true));
  assert.match(view.sideEffectWarning!,/重放|核对|确认/);
});

test("unknown receipt state fails closed without automatic replay",()=>{
  assert.match(resultView(receipt("future_state" as any,false)).sideEffectWarning!,/核对|重放/);
});
test("old-run receipt cannot borrow current-run task view",()=>{
  assert.throws(()=>resultView(receipt("verified",true),{local_task_id:"t",current_run_id:"new_run"} as any),/运行身份/);
});
