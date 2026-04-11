// ===== 定数・設定 =====
const LEVEL_LABELS = {
  national:   '国政',
  pref:       '都道府県',
  city:       '市・区',
  town:       '町・村',
  unexpected: '補選・急選',
};

// フィルター状態
let currentLevel = 'all';
let showCompleted = false;
let electionsData = [];
let dietData = [];

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
  if (!next) {
    document.getElementById('countdown-name').textContent = '予定された選挙はありません';
    document.getElementById('countdown-days').textContent = '--';
    return;
  }

  document.getElementById('countdown-name').textContent = next.name;

  const elDay = getPrimaryDate(next);
  const days = Math.ceil((elDay - today()) / 86400000);
  document.getElementById('countdown-days').textContent = days >= 0 ? days : 0;

  // 投票日の表示
  const elDayLabel = getElectionDayLabel(next);
  document.getElementById('countdown-detail').textContent = `投票日: ${elDayLabel}`;

  // 確定度
  const certMap = { confirmed: '', estimated: '※ 日程は予測値です', unknown: '※ 日程未確定' };
  document.getElementById('countdown-certainty').textContent = certMap[next.certainty] || '';

  // 公示日カウントダウン
  if (next.announcementDate) {
    const annDays = daysUntil(next.announcementDate);
    if (annDays > 0) {
      document.getElementById('countdown-detail').textContent +=
        `　　公示まであと ${annDays} 日`;
    }
  }

  // カードの色（緊急度）
  const card = document.getElementById('countdown-card');
  if (days <= 7) card.style.background = 'rgba(192,57,43,0.25)';
  else if (days <= 30) card.style.background = 'rgba(230,126,34,0.2)';
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
  const levelLabel = el.isUnexpected ? '補選・急選' : (LEVEL_LABELS[el.level] || el.level);
  const levelClass = el.isUnexpected ? 'unexpected' : el.level;
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

// ===== メインレンダリング =====
function render() {
  const filtered = getFiltered();
  const sorted = [...filtered].sort((a, b) =>
    (getPrimaryDate(a) || new Date(9999,0)) - (getPrimaryDate(b) || new Date(9999,0))
  );

  renderCountdown(electionsData.filter(el => !isCompleted(el)));
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
    renderTimeline(getFiltered().sort((a, b) =>
      (getPrimaryDate(a) || new Date(9999,0)) - (getPrimaryDate(b) || new Date(9999,0))
    ));
  });
});

document.getElementById('show-completed').addEventListener('change', e => {
  showCompleted = e.target.checked;
  renderTimeline(getFiltered().sort((a, b) =>
    (getPrimaryDate(a) || new Date(9999,0)) - (getPrimaryDate(b) || new Date(9999,0))
  ));
});

// ===== 起動 =====
loadData();
