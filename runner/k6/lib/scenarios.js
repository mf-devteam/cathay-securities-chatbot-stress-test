// 業務情境：只做「業務情境組合」中標黃色的列（A4、B1、B2、B2-2、B3、B4、B5、C1）。
//   - 有流量占比的 B2、B2-2、B3、B4、B5、C1 依 SCENARIO_WEIGHTS 加權抽選（每則訊息抽一次）
//   - B1 開啟聊天：每段對話開頭的開場問候（/ask，isInitialized=true），不另計占比
//   - A4 Token 續期：需要登入，不在本次範圍的流程內，只在 Soak 時觀察受測端
// flow 決定套用哪一組門檻：AI 情境用 FAQ 門檻，非 AI 情境用業務查詢門檻（報表 Job 依 flow 分開統計）
import { SCENARIO_WEIGHTS } from './config.js';

export const CATALOG = {
    B2: { name: 'FAQ 問答', flow: 'faq' },
    'B2-2': { name: 'FAQ intent', flow: 'business' },
    B3: { name: '多輪對話', flow: 'faq' },
    B4: { name: '知識庫以外問題', flow: 'faq' },
    B5: { name: 'Model Armor 攔截', flow: 'faq', expectStatus: 205 },
    C1: { name: '查看 ETF 清單', flow: 'business' },
};

export const OPENING = { id: 'B1', name: '開啟聊天（開場問候）', flow: 'init', expectStatus: 200 };

export const ACTIVE = Object.entries(SCENARIO_WEIGHTS)
    .filter(([, weight]) => weight > 0)
    .map(([id, weight]) => {
        if (!CATALOG[id]) throw new Error(`SCENARIO_WEIGHTS 有未定義的情境：${id}（可用：${Object.keys(CATALOG).join('、')}）`);
        return { id, weight, expectStatus: 200, ...CATALOG[id] };
    });
if (ACTIVE.length === 0) throw new Error('SCENARIO_WEIGHTS 至少要有一個情境的權重大於 0');

const TOTAL_WEIGHT = ACTIVE.reduce((sum, s) => sum + s.weight, 0);

export function pickScenario() {
    let r = Math.random() * TOTAL_WEIGHT;
    for (const s of ACTIVE) {
        r -= s.weight;
        if (r <= 0) return s;
    }
    return ACTIVE[ACTIVE.length - 1];
}

// 正規化後的占比（摘要與啟動紀錄用）
export const SHARES = ACTIVE.map((s) => `${s.id}=${((s.weight / TOTAL_WEIGHT) * 100).toFixed(1)}%`).join(' ');
