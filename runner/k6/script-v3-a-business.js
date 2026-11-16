// v3 腳本 A：業務查詢（預設 B2-2 FAQ intent、C1 查看 ETF 清單；情境以 V3_A_WEIGHTS 為準）；回應時間門檻 V3_A_MAX_MS（預設 6 秒）
// 由 control 的 k6suite 依序執行（A → 休息 → B → 合併報表），邏輯在 lib/v3.js。
import { buildPart } from './lib/v3.js';
import { stageMarker } from './lib/stages.js';

const part = buildPart('A', () => JSON.parse(open('./data/questions_v3.json')));

export { stageMarker };
export const options = part.options;
export const setup = part.setup;
export default part.run;
export const handleSummary = part.handleSummary;
