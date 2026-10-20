// 逐階加壓：STAGE_PLAN → k6 ramping-arrival-rate 的 stages，並替每個請求標上 stage／phase tag（報表 Job 依此分階段）
import { sleep } from 'k6';
import exec from 'k6/execution';
import { BASE_CONV_RATE, CALLS_PER_CONV, RUN_ID, STAGE_PLAN, STAGE_SCOPE, TIME_SCALE } from './config.js';

export const ACTIVE_STAGES = STAGE_SCOPE === 'smoke' ? STAGE_PLAN.slice(0, 1) : STAGE_PLAN;

const scaledSeconds = (min) => Math.max(1, Math.round((min * 60) / TIME_SCALE));
// 爬升 0 分鐘＝直接跳到目標流量（k6 stage 仍需 ≥ 1 秒）
const rampSeconds = (s) => (s.rampMin > 0 ? scaledSeconds(s.rampMin) : 1);

// ramping-arrival-rate 的 target 須為整數，以「每小時新開對話數」表示
const convPerHour = (pct) => Math.max(1, Math.round(pct * BASE_CONV_RATE * 3600));

export function buildStages() {
    const stages = [];
    for (const s of ACTIVE_STAGES) {
        stages.push({ target: convPerHour(s.pct), duration: `${rampSeconds(s)}s` });
        stages.push({ target: convPerHour(s.pct), duration: `${scaledSeconds(s.holdMin)}s` });
    }
    return stages;
}

// 每段（爬升／持續）的起訖秒數
const SCHEDULE = (() => {
    const out = [];
    let t = 0;
    for (const s of ACTIVE_STAGES) {
        const ramp = rampSeconds(s);
        const hold = scaledSeconds(s.holdMin);
        out.push({ stage: s, phase: 'ramp', startSec: t, endSec: t + ramp });
        out.push({ stage: s, phase: 'hold', startSec: t + ramp, endSec: t + ramp + hold });
        t += ramp + hold;
    }
    return out;
})();

export const TOTAL_SECONDS = SCHEDULE[SCHEDULE.length - 1].endSec;

export function stageTags() {
    const elapsed = exec.instance.currentTestRunDuration / 1000;
    for (const seg of SCHEDULE) {
        if (elapsed < seg.endSec) return { stage: seg.stage.id, phase: seg.phase };
    }
    return { stage: 'drain', phase: 'drain' }; // 階段結束後、gracefulStop 期間仍在跑的對話
}

// 階段切換事件：由單一 VU 在每個階段開始時寫一行，runner 轉送到 Cloud Logging，觸發「進入新階段」通知
export function stageMarker() {
    for (const seg of SCHEDULE.filter((s) => s.phase === 'ramp')) {
        const wait = seg.startSec - exec.instance.currentTestRunDuration / 1000;
        if (wait > 0) sleep(wait);
        const conv = seg.stage.pct * BASE_CONV_RATE;
        console.log(
            `STRESS_TEST_EVENT=STAGE run_id=${RUN_ID} stage=${seg.stage.id} pct=${seg.stage.pct} ` +
                `conv_per_s=${conv.toFixed(2)} api_per_s=${(conv * CALLS_PER_CONV).toFixed(0)} hold_min=${seg.stage.holdMin} time_scale=${TIME_SCALE}`
        );
    }
}
