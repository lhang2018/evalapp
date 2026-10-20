/** AsyncStorage → KeyValueStore 适配（离线层的唯一平台依赖）。 */

import AsyncStorage from "@react-native-async-storage/async-storage";
import type { KeyValueStore } from "../offline/storage.ts";

export const asyncStorageStore: KeyValueStore = {
  getItem: (key) => AsyncStorage.getItem(key),
  setItem: (key, value) => AsyncStorage.setItem(key, value),
  removeItem: (key) => AsyncStorage.removeItem(key),
};
