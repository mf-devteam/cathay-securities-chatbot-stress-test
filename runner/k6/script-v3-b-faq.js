// v3 腳本 B：FAQ 問答（預設 B2 FAQ 問答、B5 Model Armor 攔截；情境以 V3_B_WEIGHTS 為準）；回應時間門檻 V3_B_MAX_MS（預設 20 秒）
// 由 control 的 k6suite 依序執行（A → 休息 → B → 合併報表），邏輯在 lib/v3.js。
import { buildPart } from './lib/v3.js';
import { stageMarker } from './lib/stages.js';

const part = buildPart('B', () => JSON.parse(open('./data/questions_v3.json')));

export { stageMarker };
export const options = part.options;
export const setup = part.setup;
export default part.run;
export const handleSummary = part.handleSummary;
