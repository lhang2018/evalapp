import { AppRegistry } from "react-native";
import App from "./App";
import { BACKGROUND_SYNC_TASK } from "./src/offline/background";
import { runStoredBackgroundSync } from "./src/offline/backgroundRunner";

AppRegistry.registerComponent("EvoAgent", () => App);
// WorkManager 唤醒的无界面任务：只复用持久化队列，不打开 UI
AppRegistry.registerHeadlessTask(BACKGROUND_SYNC_TASK, () => runStoredBackgroundSync);
