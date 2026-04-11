// ===== 定数・設定 =====
const LEVEL_LABELS = {
  national:   '国政',
  pref:       '都道府県',
  city:       '市区町村',
  town:       '市区町村',
  unexpected: '補選・急選',
};

// フィルター状態
let currentLevel = 'all';
let showCompleted = false;
let currentView = 'timeline';
let electionsData = [];
let dietData = [];

// ガントチャートの表示開始月オフセット（0 = 今月）
let ganttOffset = 0;
const GANTT_MONTHS = 12; // 一度に表示する月数

// ===== データ読み込み =====
async function loadData() {
  try {
    const [eRes, dRes] = await Promise.all([
      fetch('data/elections.json'),
      fetch('data/diet.json'),
    ]);
    const eJson = await eRes.json();
    const dJson = await dRes.json();
    electionsData = eJson.elections || [];
    dietData = dJson.sessions || [];
    document.getElementById('last-updated').textContent =
      formatDatetime(eJson.lastUpdated);
    render();
  } catch (e) {
    console.error('データ読み込みエラー:', e);
    document.getElementById('timeline').innerHTML =
      '<p class="loading-text">データの読み込みに失敗しました。</p>';
  }
}

// ===== 選挙の色クラスを返す =====
function getLevelClass(el) {
  if (el.isUnexpected) return 'unexpected';
  if (el.level === 'national') return 'national';
  if (el.level === 'pref') {
    if (/知事/.test(el.type)) return 'pref-mayor';
    if (/県議|道議|府議|都議|議会/.test(el.type)) return 'pref-assembly';
    return 'pref-mayor'; // デフォルト
  }
  if (el.level === 'city' || el.level === 'town') {
    if (/市長|町長|村長|区長/.test(el.type)) return 'city-mayor';
    if (/市議|町議|村議|区議|議会/.test(el.type)) return 'city-assembly';
    return 'city-mayor'; // デフォルト
  }
  return el.level;
}

// ===== 日付ユーティリティ =====
function today() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d;
}

function parseDate(str) {
  if (!str) return null;
  const [y, m, d] = str.split('-').map(Number);
  return new Date(y, m - 1, d);
}

function daysUntil(dateStr) {
  const d = parseDate(dateStr);
  if (!d) return null;
  return Math.ceil((d - today()) / 86400000);
}

function formatDate(dateStr) {
  const d = parseDate(dateStr);
  if (!d) return null;
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}

function formatDatetime(str) {
  if (!str) return '不明';
  const d = new Date(str);
  return `${d.getFullYear()}/${String(d.getMonth()+1).padStart(2,'0')}/${String(d.getDate()).padStart(2,'0')} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
}

// 選挙の「代表日」を返す（カウントダウン・ソート用）
// 確定日優先、なければ推定レンジの中央値
function getPrimaryDate(el) {
  if (el.electionDay) return parseDate(el.electionDay);
  if (el.electionDayEarliest && el.electionDayLatest) {
    const a = parseDate(el.electionDayEarliest);
    const b = parseDate(el.electionDayLatest);
    return new Date((a.getTime() + b.getTime()) / 2);
  }
  if (el.electionDayEarliest) return parseDate(el.electionDayEarliest);
  return null;
}

function isCompleted(el) {
  const d = getPrimaryDate(el);
  return d && d < today();
}

// ===== フィルタリング =====
function getFiltered() {
  return electionsData.filter(el => {
    if (!showCompleted && isCompleted(el)) return false;
    if (currentLevel === 'all') return true;
    if (currentLevel === 'unexpected') return el.isUnexpected;
    if (currentLevel === 'pref') return el.level === 'pref';
    if (currentLevel === 'pref-mayor')    return el.level === 'pref' && /知事/.test(el.type);
    if (currentLevel === 'pref-assembly') return el.level === 'pref' && /県議|道議|府議|都議|議会/.test(el.type);
    if (currentLevel === 'municipal') return el.level === 'city' || el.level === 'town';
    if (currentLevel === 'mayor')     return (el.level === 'city' || el.level === 'town') && /市長|町長|村長|区長/.test(el.type);
    if (currentLevel === 'assembly')  return (el.level === 'city' || el.level === 'town') && /市議|町議|村議|区議|議会/.test(el.type);
    return el.level === currentLevel;
  });
}

// ===== 次の注目選挙（カウントダウン用） =====
function getNextElection(elections) {
  const future = elections
    .filter(el => !isCompleted(el))
    .map(el => ({ el, d: getPrimaryDate(el) }))
    .filter(x => x.d)
    .sort((a, b) => a.d - b.d);
  return future.length ? future[0].el : null;
}

// ===== カウントダウンレンダリング =====
function renderCountdown(elections) {
  const next = getNextElection(elections.length ? elections : electionsData.filter(el => !isCompleted(el)));
  const card = document.getElementById('countdown-card');

  if (!next) {
    document.getElementById('countdown-name').textContent = '予定された選挙はありません';
    document.getElementById('countdown-days').textContent = '--';
    return;
  }

  document.getElementById('countdown-name').textContent = next.name;

  const elDay = getPrimaryDate(next);
  const days = Math.ceil((elDay - today()) / 86400000);
  document.getElementById('countdown-days').textContent = days >= 0 ? days : 0;

  // 詳細情報を複数行で表示
  const lines = [];

  // 投票日
  lines.push(`投票日　${getElectionDayLabel(next)}`);

  // 公示日
  const annLabel = getAnnouncementLabel(next);
  if (annLabel !== '未定') lines.push(`公示日　${annLabel}`);

  // 公示日までのカウントダウン
  if (next.announcementDate) {
    const annDays = daysUntil(next.announcementDate);
    if (annDays > 0) lines.push(`公示まであと ${annDays} 日`);
  }

  // 地域
  if (next.region && next.region !== '全国') lines.push(`地域　${next.region}`);

  document.getElementById('countdown-detail').innerHTML = lines.join('<br>');

  // 確定度
  const certMap = { confirmed: '', estimated: '日程は予測値', unknown: '日程未確定' };
  document.getElementById('countdown-certainty').textContent = certMap[next.certainty] || '';

  // カードの色（緊急度）
  if (days <= 7) card.style.background = 'rgba(192,57,43,0.3)';
  else if (days <= 30) card.style.background = 'rgba(230,126,34,0.2)';
  else card.style.background = '';
}

// ===== 主要3選挙（衆院選・参院選・統一地方選）=====
const MAJOR_ELECTION_MATCHERS = [
  {
    label: '衆院選',
    match: el => el.level === 'national' && /衆議院|衆院選/.test(el.name + el.type),
    unknownLabel: '解散次第',
  },
  {
    label: '参院選',
    match: el => el.level === 'national' && /参議院|参院選/.test(el.name + el.type),
    unknownLabel: '未定',
  },
  {
    label: '統一地方選',
    match: el => /統一地方選/.test(el.name + el.type),
    unknownLabel: '未定',
  },
];

function renderMajorElections() {
  const container = document.getElementById('major-elections');
  const allFuture = electionsData.filter(el => !isCompleted(el));

  container.innerHTML = MAJOR_ELECTION_MATCHERS.map(({ label, match, unknownLabel }) => {
    // 同種の中で最も近いものを選ぶ
    const candidates = allFuture
      .filter(match)
      .map(el => ({ el, d: getPrimaryDate(el) }))
      .filter(x => x.d)
      .sort((a, b) => a.d - b.d);

    const found = candidates[0];

    if (!found) {
      return `
        <div class="major-card">
          <div class="major-card-label">${label}</div>
          <div class="major-card-name">${label}</div>
          <div class="major-card-unknown">${unknownLabel}</div>
          <div class="major-card-date">日程未定</div>
        </div>`;
    }

    const { el } = found;
    const days = Math.ceil((found.d - today()) / 86400000);
    const uncertain = el.certainty !== 'confirmed';
    const dateLabel = getElectionDayLabel(el);
    const isDissolution = !!el.dissolutionRisk;

    return `
      <div class="major-card">
        <div class="major-card-label">${label}</div>
        <div class="major-card-name">${isDissolution ? '任期満了まで' : label}</div>
        <div>
          <span class="major-card-days">${days}</span>
          <span class="major-card-unit">日</span>
        </div>
        <div class="major-card-date">${dateLabel}</div>
        ${uncertain && !isDissolution ? '<div class="major-card-uncertain">※予測値</div>' : ''}
        ${isDissolution ? '<div class="major-card-dissolution">※いつでも解散の可能性あり</div>' : ''}
      </div>`;
  }).join('');
}

// ===== 直近サブカード =====
function renderUpcomingMini(elections) {
  const future = elections
    .filter(el => !isCompleted(el))
    .map(el => ({ el, d: getPrimaryDate(el) }))
    .filter(x => x.d)
    .sort((a, b) => a.d - b.d)
    .slice(0, 3);

  const container = document.getElementById('upcoming-mini');
  container.innerHTML = future.map(({ el }) => {
    const days = Math.ceil((getPrimaryDate(el) - today()) / 86400000);
    const uncertain = el.certainty !== 'confirmed';
    return `
      <div class="mini-card">
        <div class="mini-card-left">
          <div class="mini-card-name">
            ${el.name}
            ${el.isUnexpected ? '<span class="mini-badge unexpected">速報</span>' : ''}
          </div>
          <div class="mini-card-date">${getElectionDayLabel(el)}${uncertain ? '（予測）' : ''}</div>
        </div>
        <div class="mini-card-days">あと${days >= 0 ? days : 0}<span>日</span></div>
      </div>
    `;
  }).join('');
}

// ===== ラベル生成 =====
function getAnnouncementLabel(el) {
  if (el.announcementDate) {
    return formatDate(el.announcementDate);
  }
  if (el.announcementDateLabel) return el.announcementDateLabel;
  if (el.announcementDateEarliest && el.announcementDateLatest) {
    return `${formatDate(el.announcementDateEarliest)} 〜 ${formatDate(el.announcementDateLatest)}頃`;
  }
  return '未定';
}

function getElectionDayLabel(el) {
  if (el.electionDay) return formatDate(el.electionDay);
  if (el.electionDayLabel) return el.electionDayLabel;
  if (el.electionDayEarliest && el.electionDayLatest) {
    return `${formatDate(el.electionDayEarliest)} 〜 ${formatDate(el.electionDayLatest)}頃`;
  }
  return '未定';
}

// ===== カードカウントダウン表示 =====
function renderCardCountdown(el) {
  if (isCompleted(el)) {
    return `<div class="completed-label">終了</div>`;
  }
  const d = getPrimaryDate(el);
  if (!d) return `<div class="card-countdown-label">日程未定</div>`;
  const days = Math.ceil((d - today()) / 86400000);
  const urgent = days <= 14;
  const uncertain = el.certainty !== 'confirmed';
  return `
    <div class="card-countdown${urgent ? ' urgent' : ''}">あと${days}日</div>
    <div class="card-countdown-label">投票日まで</div>
    ${uncertain ? '<div class="card-countdown-sub">※予測値</div>' : ''}
  `;
}

// ===== 選挙カード =====
function renderElectionCard(el) {
  const levelClass = getLevelClass(el);
  const levelLabel = el.isUnexpected ? '補選・急選' : (el.type || LEVEL_LABELS[el.level] || el.level);
  const annLabel = getAnnouncementLabel(el);
  const elDayLabel = getElectionDayLabel(el);
  const isUnc = el.certainty !== 'confirmed';
  const completed = isCompleted(el);

  return `
    <div class="election-card ${levelClass}${completed ? ' completed' : ''}">
      <div class="card-main">
        <div class="card-title">
          ${el.name}
          <span class="level-badge ${levelClass}">${levelLabel}</span>
          ${el.isUnexpected ? '<span class="unexpected-tag">⚡ 速報</span>' : ''}
        </div>
        <div class="card-dates">
          <div class="date-item">
            <div class="date-item-label">公示日</div>
            <div class="date-item-value${isUnc && !el.announcementDate ? ' date-uncertain' : ''}">
              ${annLabel}
            </div>
            ${isUnc && el.announcementDate ? '<div class="certainty-note">※予測値</div>' : ''}
          </div>
          <div class="date-item">
            <div class="date-item-label">投票日</div>
            <div class="date-item-value${isUnc && !el.electionDay ? ' date-uncertain' : ''}">
              ${elDayLabel}
            </div>
            ${isUnc && el.electionDay ? '<div class="certainty-note">※予測値</div>' : ''}
          </div>
          ${el.region && el.region !== '全国' ? `
            <div class="date-item">
              <div class="date-item-label">地域</div>
              <div class="date-item-value">${el.region}</div>
            </div>` : ''}
        </div>
        ${el.note ? `<div class="card-note">${el.note}</div>` : ''}
        ${(el.seats || el.candidateCount) ? `
          <div class="election-meta">
            ${el.seats ? `<span class="meta-item">定数 <strong>${el.seats}</strong>人</span>` : ''}
            ${el.candidateCount ? `<span class="meta-item">候補者 <strong>${el.candidateCount}</strong>人</span>` : ''}
          </div>` : ''}
        ${el.competitiveness ? `
          <div class="competitiveness-wrap">
            <span class="competitiveness-badge level-${el.competitiveness.level || 'unknown'}">${el.competitiveness.label || '?'}</span>
            ${el.competitiveness.note ? `<span class="competitiveness-note">${el.competitiveness.note}</span>` : ''}
          </div>` : ''}
      </div>
      <div class="card-right">
        ${renderCardCountdown(el)}
      </div>
    </div>
  `;
}

// ===== タイムラインレンダリング =====
function renderTimeline(elections) {
  const container = document.getElementById('timeline');

  if (!elections.length) {
    container.innerHTML = '<p class="no-elections">該当する選挙はありません。</p>';
    return;
  }

  // 月別グループ化
  const groups = {};
  elections.forEach(el => {
    const d = getPrimaryDate(el);
    const key = d
      ? `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
      : '9999-99';
    if (!groups[key]) groups[key] = [];
    groups[key].push(el);
  });

  const sortedKeys = Object.keys(groups).sort();

  container.innerHTML = sortedKeys.map(key => {
    const label = key === '9999-99' ? '日程未定' :
      (() => {
        const [y, m] = key.split('-');
        return `${y}年 ${parseInt(m)}月`;
      })();

    return `
      <div class="month-group">
        <div class="month-label">${label}</div>
        ${groups[key]
          .sort((a, b) => (getPrimaryDate(a) || new Date(9999,0)) - (getPrimaryDate(b) || new Date(9999,0)))
          .map(renderElectionCard).join('')}
      </div>
    `;
  }).join('');
}

// ===== 国会日程レンダリング =====
function renderDiet() {
  const container = document.getElementById('diet-schedule');
  if (!dietData.length) {
    container.innerHTML = '<p class="loading-text">国会日程データなし</p>';
    return;
  }

  const now = today();

  container.innerHTML = dietData.map(s => {
    const open = parseDate(s.openDate);
    const close = parseDate(s.closeDate);
    const isActive = open <= now && now <= close;
    const isEnded = close < now;

    const milestoneHTML = (s.milestones && s.milestones.length) ? `
      <div class="milestones">
        <div class="milestone-label">主要日程</div>
        ${s.milestones.map(m => `
          <div class="milestone-item">
            <span class="milestone-date">${formatDate(m.date)}</span>
            <span class="milestone-name">${m.label}</span>
          </div>
        `).join('')}
      </div>` : '';

    const closeLabelRaw = s.closeDateUncertain ? `${formatDate(s.closeDate)}（予定）` : formatDate(s.closeDate);

    return `
      <div class="session-card">
        <div class="session-name">
          ${s.name}
          <span class="session-type-badge">${s.type}</span>
        </div>
        ${isActive ? '<div class="session-active">● 開会中</div>' : ''}
        ${isEnded ? '<div class="session-ended">閉会</div>' : ''}
        <div class="session-dates">
          <div class="session-date-row">
            <span class="session-date-label">開会</span>
            <span class="session-date-value">${formatDate(s.openDate)}</span>
          </div>
          <div class="session-date-row">
            <span class="session-date-label">閉会</span>
            <span class="session-date-value">${closeLabelRaw}</span>
          </div>
        </div>
        ${milestoneHTML}
      </div>
    `;
  }).join('');
}

// ===== ガントチャート =====

// 表示する月リストを生成（ganttOffset から GANTT_MONTHS ヶ月分）
function getGanttMonths() {
  const t = today();
  const months = [];
  for (let i = ganttOffset; i < ganttOffset + GANTT_MONTHS; i++) {
    const d = new Date(t.getFullYear(), t.getMonth() + i, 1);
    months.push(d);
  }
  return months;
}

// 月の日数
function daysInMonth(year, month) {
  return new Date(year, month + 1, 0).getDate();
}

// 選挙のガント上の「開始日」「終了日」を取得
function getGanttSpan(el) {
  const start = el.announcementDate
    ? parseDate(el.announcementDate)
    : (el.announcementDateEarliest ? parseDate(el.announcementDateEarliest) : null);
  const end = el.electionDay
    ? parseDate(el.electionDay)
    : (el.electionDayLatest ? parseDate(el.electionDayLatest) : null);
  // 公示日なし・投票日のみの場合
  if (!start && end) return { start: end, end };
  if (!end && start) return { start, end: start };
  return { start, end };
}

function renderGantt() {
  const elections = getFiltered().filter(el => {
    const span = getGanttSpan(el);
    return span.start || span.end;
  }).sort((a, b) =>
    (getPrimaryDate(a) || new Date(9999,0)) - (getPrimaryDate(b) || new Date(9999,0))
  );

  const months = getGanttMonths();
  const firstDay = months[0];
  const lastDay = new Date(months[months.length - 1].getFullYear(), months[months.length - 1].getMonth() + 1, 0);
  const todayDate = today();

  // 範囲ラベル更新
  const firstM = months[0];
  const lastM = months[months.length - 1];
  document.getElementById('gantt-range-label').textContent =
    `${firstM.getFullYear()}年${firstM.getMonth()+1}月 〜 ${lastM.getFullYear()}年${lastM.getMonth()+1}月`;

  // 表示期間の総日数
  const totalDays = Math.round((lastDay - firstDay) / 86400000) + 1;

  // セル幅計算（月ヘッダーセルの比率）
  const monthWidths = months.map(m => daysInMonth(m.getFullYear(), m.getMonth()));

  // テーブル構築
  let html = '<table class="gantt-table">';

  // 年ヘッダー行
  html += '<colgroup><col style="width:160px">';
  months.forEach(m => {
    html += `<col style="width:${(daysInMonth(m.getFullYear(), m.getMonth()) / totalDays * 100).toFixed(2)}%">`;
  });
  html += '</colgroup>';

  // 年ラベル（月が変わるところでまとめる）
  html += '<thead><tr class="gantt-year-row"><th></th>';
  let prevYear = null;
  let yearSpan = 0;
  const yearGroups = [];
  months.forEach((m, i) => {
    if (m.getFullYear() !== prevYear) {
      if (prevYear !== null) yearGroups.push({ year: prevYear, span: yearSpan });
      prevYear = m.getFullYear();
      yearSpan = 1;
    } else {
      yearSpan++;
    }
    if (i === months.length - 1) yearGroups.push({ year: prevYear, span: yearSpan });
  });
  yearGroups.forEach(g => {
    html += `<th colspan="${g.span}">${g.year}年</th>`;
  });
  html += '</tr>';

  // 月ヘッダー行
  html += '<tr class="gantt-header-row"><th>選挙名</th>';
  months.forEach(m => {
    const isTodayMonth = m.getFullYear() === todayDate.getFullYear() && m.getMonth() === todayDate.getMonth();
    html += `<th${isTodayMonth ? ' class="today-col"' : ''}>${m.getMonth()+1}月</th>`;
  });
  html += '</tr></thead><tbody>';

  // 各選挙行
  if (!elections.length) {
    html += `<tr><td colspan="${months.length + 1}" style="padding:24px;text-align:center;color:#999">該当する選挙はありません</td></tr>`;
  }

  elections.forEach(el => {
    const span = getGanttSpan(el);
    const levelClass = getLevelClass(el);
    const isUnc = el.certainty !== 'confirmed';
    const completed = isCompleted(el);

    html += `<tr class="gantt-row${completed ? ' completed' : ''}">`;
    html += `<td class="gantt-label-cell" title="${el.name}">
      <div class="gantt-label-name">${el.name}</div>
      ${el.region && el.region !== '全国' ? `<div class="gantt-label-region">${el.region}</div>` : ''}
    </td>`;

    // 各月セル
    months.forEach(m => {
      const year = m.getFullYear();
      const month = m.getMonth();
      const mStart = new Date(year, month, 1);
      const mEnd = new Date(year, month + 1, 0);
      const mDays = daysInMonth(year, month);
      const isTodayMonth = year === todayDate.getFullYear() && month === todayDate.getMonth();

      html += `<td class="gantt-cell${isTodayMonth ? ' today-col' : ''}" style="position:relative">`;

      // バーがこのセルに掛かるか判定
      if (span.start && span.end) {
        const barStart = span.start < mStart ? mStart : span.start;
        const barEnd   = span.end   > mEnd   ? mEnd   : span.end;

        if (barStart <= barEnd && span.start <= mEnd && span.end >= mStart) {
          // セル内での開始・終了の割合（%）
          const leftPct  = ((barStart - mStart) / 86400000 / mDays * 100).toFixed(2);
          const rightPct = ((mEnd - barEnd)      / 86400000 / mDays * 100).toFixed(2);
          const widthPct = (100 - parseFloat(leftPct) - parseFloat(rightPct)).toFixed(2);

          // 月またぎのとき左右の角丸を制御
          const isBarStart = span.start >= mStart;
          const isBarEnd   = span.end   <= mEnd;
          const rl = isBarStart ? '4px' : '0';
          const rr = isBarEnd   ? '4px' : '0';

          html += `<div class="gantt-bar ${levelClass}${isUnc ? ' uncertain' : ''}"
            style="left:${leftPct}%;width:${widthPct}%;min-width:4px;border-radius:${rl} ${rr} ${rr} ${rl};"
            title="${el.name}（${getAnnouncementLabel(el)} 〜 ${getElectionDayLabel(el)}）">`;

          // 公示日マーカー（このセル内に公示日があれば）
          if (span.start >= mStart && span.start <= mEnd) {
            const markerLeft = ((span.start - mStart) / 86400000 / mDays * 100).toFixed(2);
            html += `<div class="gantt-marker" style="left:0"></div>`;
          }

          // バーラベル（最初の月セルにだけ表示）
          if (span.start >= mStart && span.start <= mEnd) {
            html += `<span class="gantt-bar-label">${el.name}</span>`;
          }

          html += '</div>';
        }
      }

      // 今日ライン
      if (isTodayMonth) {
        const todayLeft = ((todayDate.getDate() - 1) / mDays * 100).toFixed(2);
        html += `<div class="gantt-today-line" style="left:${todayLeft}%"></div>`;
      }

      html += '</td>';
    });

    html += '</tr>';
  });

  // 国会会期行
  dietData.forEach(s => {
    const sOpen  = parseDate(s.openDate);
    const sClose = parseDate(s.closeDate);
    if (!sOpen || !sClose) return;

    html += `<tr class="gantt-row" style="opacity:0.7">`;
    html += `<td class="gantt-label-cell" title="${s.name}">
      <div class="gantt-label-name" style="color:#7d3c98">🏛 ${s.name}</div>
      <div class="gantt-label-region">${s.type}</div>
    </td>`;

    months.forEach(m => {
      const year = m.getFullYear();
      const month = m.getMonth();
      const mStart = new Date(year, month, 1);
      const mEnd   = new Date(year, month + 1, 0);
      const mDays  = daysInMonth(year, month);
      const isTodayMonth = year === todayDate.getFullYear() && month === todayDate.getMonth();

      html += `<td class="gantt-cell${isTodayMonth ? ' today-col' : ''}" style="position:relative">`;

      if (sOpen <= mEnd && sClose >= mStart) {
        const barStart = sOpen  < mStart ? mStart : sOpen;
        const barEnd   = sClose > mEnd   ? mEnd   : sClose;
        const leftPct  = ((barStart - mStart) / 86400000 / mDays * 100).toFixed(2);
        const rightPct = ((mEnd - barEnd)      / 86400000 / mDays * 100).toFixed(2);
        const widthPct = (100 - parseFloat(leftPct) - parseFloat(rightPct)).toFixed(2);
        const isDietStart = sOpen  >= mStart;
        const isDietEnd   = sClose <= mEnd;
        const drl = isDietStart ? '3px' : '0';
        const drr = isDietEnd   ? '3px' : '0';
        html += `<div class="gantt-diet-bar" style="left:${leftPct}%;width:${widthPct}%;min-width:4px;border-radius:${drl} ${drr} ${drr} ${drl}"></div>`;
      }

      if (isTodayMonth) {
        const todayLeft = ((todayDate.getDate() - 1) / mDays * 100).toFixed(2);
        html += `<div class="gantt-today-line" style="left:${todayLeft}%"></div>`;
      }

      html += '</td>';
    });

    html += '</tr>';
  });

  html += '</tbody></table>';
  document.getElementById('gantt-container').innerHTML = html;
}

// ===== ビュー切替 =====
function switchView(view) {
  currentView = view;
  const timelineEl = document.getElementById('view-timeline');
  const ganttEl    = document.getElementById('view-gantt');
  const dietSidebar = document.getElementById('diet-sidebar');
  const mainContent = document.getElementById('main-content');

  if (view === 'gantt') {
    timelineEl.style.display = 'none';
    dietSidebar.style.display = 'none';
    mainContent.style.display = 'none';
    ganttEl.style.display = 'block';
    renderGantt();
  } else {
    timelineEl.style.display = '';
    dietSidebar.style.display = '';
    mainContent.style.display = '';
    ganttEl.style.display = 'none';
  }
}

// ===== メインレンダリング =====
function render() {
  const filtered = getFiltered();
  const sorted = [...filtered].sort((a, b) =>
    (getPrimaryDate(a) || new Date(9999,0)) - (getPrimaryDate(b) || new Date(9999,0))
  );

  renderCountdown(electionsData.filter(el => !isCompleted(el)));
  renderMajorElections();
  renderUpcomingMini(electionsData);
  renderTimeline(sorted);
  renderDiet();
}

// ===== イベントリスナー =====
document.querySelectorAll('.filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentLevel = btn.dataset.level;
    if (currentView === 'gantt') {
      renderGantt();
    } else {
      renderTimeline(getFiltered().sort((a, b) =>
        (getPrimaryDate(a) || new Date(9999,0)) - (getPrimaryDate(b) || new Date(9999,0))
      ));
    }
  });
});

document.querySelectorAll('.view-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    switchView(btn.dataset.view);
  });
});

document.getElementById('show-completed').addEventListener('change', e => {
  showCompleted = e.target.checked;
  if (currentView === 'gantt') {
    renderGantt();
  } else {
    renderTimeline(getFiltered().sort((a, b) =>
      (getPrimaryDate(a) || new Date(9999,0)) - (getPrimaryDate(b) || new Date(9999,0))
    ));
  }
});

document.getElementById('gantt-prev').addEventListener('click', () => {
  ganttOffset -= GANTT_MONTHS;
  renderGantt();
});
document.getElementById('gantt-next').addEventListener('click', () => {
  ganttOffset += GANTT_MONTHS;
  renderGantt();
});

// ===== 起動 =====
loadData();
