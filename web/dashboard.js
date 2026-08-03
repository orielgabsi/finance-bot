import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import { getAuth, onAuthStateChanged, signOut } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";
import {
  getFirestore, doc, getDoc, updateDoc, addDoc, onSnapshot, collection,
  query, orderBy, limit, serverTimestamp, writeBatch, runTransaction, deleteDoc,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import * as XLSX from "https://cdn.sheetjs.com/xlsx-0.20.3/package/xlsx.mjs";
import { firebaseConfig } from "./firebase-config.js";

const CAPITAL_GAINS_RATE = 0.25;
const KNOWN_SECURITY_NAMES = {
  "410393": "SPDR Gold MiniShares Trust",
  "411462": "iShares Bitcoin Trust",
  "1215771": "אי.בי.אי. סל ת״א-ביטוח",
  "1144401": "תכלית סל NASDAQ 100",
  "5112628": "אי.בי.אי. מחקה ת״א 125",
  "5141189": "אי.בי.אי. מניות תעשיות ביטחוניות ישראל",
};
const SAVINGS_TRACKS = {
  "7799": { name: "אלטשולר שחם חיסכון פלוס מניות", provider: "אלטשולר שחם" },
  "14864": { name: "אלטשולר שחם חיסכון פלוס עוקב מדדי מניות", provider: "אלטשולר שחם" },
};
const DEFAULT_PROFILE = {
  display_name: "", risk_profile: "balanced", investment_horizon: "medium",
  investment_goal: "long_term_growth", base_currency: "ILS",
};

const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
const db = getFirestore(app);
const $ = (id) => document.getElementById(id);

let chartInstance = null;
let benchmarkChartInstance = null;
let currentTelegramId = null;
let currentValuation = null;
let currentUserData = {};
let currentProfile = { ...DEFAULT_PROFILE };
let requestUnsubscribe = null;
let userUnsubscribe = null;
let txUnsubscribe = null;
let assetsUnsubscribe = null;
let currentFinancialAssets = {};
let pendingImportHoldings = null;
let pendingImportPlan = null;
let lastRenderedDeepAnalysisVersion = null;
let allocationMode = "asset";
let editingAssetId = null;
let assetAddingNew = false;
let goalEditing = false;
let profileEditing = false;

onAuthStateChanged(auth, async (user) => {
  if (!user) {
    window.location.href = "index.html";
    return;
  }
  await showLinkedStateOrPrompt(user.uid);
});

$("logout-btn").addEventListener("click", () => signOut(auth));

$("link-btn").addEventListener("click", async () => {
  const user = auth.currentUser;
  const code = $("link-code").value.trim();
  $("link-error").textContent = "";
  if (!user || !code) {
    $("link-error").textContent = "נא להזין קוד.";
    return;
  }

  try {
    const codeRef = doc(db, "link_codes", code);
    const codeSnap = await getDoc(codeRef);
    if (!codeSnap.exists() || codeSnap.data().used) throw new Error("invalid-code");
    const batch = writeBatch(db);
    batch.update(codeRef, { used: true, used_by: user.uid, used_at: serverTimestamp() });
    batch.set(doc(db, "account_links", user.uid), {
      telegram_id: codeSnap.data().telegram_id,
      used_code: code,
      linked_at: serverTimestamp(),
    });
    await batch.commit();
    await showLinkedStateOrPrompt(user.uid);
  } catch (error) {
    $("link-error").textContent = "החיבור נכשל — הקוד שגוי, נוצל כבר או שפג תוקפו. שלח /link שוב בבוט.";
    console.error(error);
  }
});

async function showLinkedStateOrPrompt(uid) {
  const linkSnap = await getDoc(doc(db, "account_links", uid));
  if (!linkSnap.exists()) {
    $("link-section").classList.remove("hidden");
    $("dashboard-section").classList.add("hidden");
    return;
  }
  $("link-section").classList.add("hidden");
  $("dashboard-section").classList.remove("hidden");
  currentTelegramId = linkSnap.data().telegram_id;
  startRealtimeSync(currentTelegramId);
}

function startRealtimeSync(telegramId) {
  if (userUnsubscribe) userUnsubscribe();
  if (txUnsubscribe) txUnsubscribe();
  if (assetsUnsubscribe) assetsUnsubscribe();
  setSyncState("connecting", "מתחבר…");

  userUnsubscribe = onSnapshot(doc(db, "users", telegramId), (snapshot) => {
    currentUserData = snapshot.exists() ? snapshot.data() : {};
    currentProfile = { ...DEFAULT_PROFILE, ...(currentUserData.profile || {}) };
    currentValuation = currentUserData.last_valuation || null;
    renderDashboard();
    setSyncState("online", "סנכרון חי");
  }, (error) => {
    setSyncState("offline", "הסנכרון נותק");
    console.error(error);
  });

  const txQuery = query(
    collection(db, "users", telegramId, "transactions"),
    orderBy("timestamp", "desc"),
    limit(30),
  );
  txUnsubscribe = onSnapshot(txQuery, (snapshot) => {
    renderTransactions(snapshot.docs.map((item) => item.data()));
  }, (error) => {
    $("tx-list").textContent = "לא ניתן לטעון פעילות כרגע.";
    console.error(error);
  });

  assetsUnsubscribe = onSnapshot(
    collection(db, "users", telegramId, "financial_assets"),
    (snapshot) => {
      const byTrack = new Map();
      for (const item of snapshot.docs) {
        const asset = { id: item.id, ...item.data() };
        const key = asset.track_id || item.id;
        const previous = byTrack.get(key);
        const stamp = asset.updated_at?.toMillis?.() || asset.created_at?.toMillis?.() || 0;
        const previousStamp = previous?.updated_at?.toMillis?.() || previous?.created_at?.toMillis?.() || 0;
        if (!previous || stamp >= previousStamp) byTrack.set(key, asset);
      }
      currentFinancialAssets = Object.fromEntries([...byTrack.values()].map((asset) => [asset.id, asset]));
      renderDashboard();
    },
    (error) => {
      showMessage("asset-message", "לא ניתן לטעון את הקופות כרגע.", false);
      console.error(error);
    },
  );
}

function renderDashboard() {
  const valuation = currentValuation;
  const holdings = valuation?.holdings || {};
  const cash = numberOrZero(currentUserData.cash_balance ?? valuation?.cash_balance);
  const securitiesValue = numberOrZero(valuation?.total_value);
  const tradingAccountValue = securitiesValue + cash;
  const savingsValue = Object.values(currentFinancialAssets).reduce(
    (sum, asset) => sum + numberOrZero(asset.estimated_balance ?? asset.reported_balance), 0,
  );
  const savingsAssetsWithGain = Object.values(currentFinancialAssets).filter((asset) => asset.estimated_gain_loss != null);
  const savingsGain = savingsAssetsWithGain.reduce((sum, asset) => sum + numberOrZero(asset.estimated_gain_loss), 0);
  const savingsGainBase = savingsAssetsWithGain.reduce(
    (sum, asset) => sum + numberOrZero(asset.estimated_balance ?? asset.reported_balance) - numberOrZero(asset.estimated_gain_loss), 0,
  );
  const savingsGainPct = savingsGainBase > 0 ? (savingsGain / savingsGainBase) * 100 : 0;
  const accountValue = tradingAccountValue + savingsValue;
  const pricedCost = numberOrZero(valuation?.priced_cost ?? valuation?.total_cost);
  const gain = numberOrZero(valuation?.total_gain_loss);
  const gainPct = numberOrZero(valuation?.total_gain_loss_pct);
  const dayChange = numberOrZero(valuation?.total_day_change_value);

  $("welcome-title").textContent = currentProfile.display_name
    ? `שלום ${currentProfile.display_name}, הנה התמונה המלאה`
    : "שלום, הנה התמונה המלאה";
  $("stat-account-value").textContent = formatMoney(accountValue);
  $("stat-trading-account").textContent = formatMoney(tradingAccountValue);
  $("stat-value").textContent = formatMoney(securitiesValue);
  $("stat-cash").textContent = formatMoney(cash);
  $("stat-savings").textContent = formatMoney(savingsValue);
  $("stat-cost").textContent = formatMoney(pricedCost);
  $("stat-gain").textContent = formatSignedMoneyAndPercent(gain, gainPct);
  setTrendClass($("stat-gain"), gain);
  $("stat-savings-gain").textContent = savingsAssetsWithGain.length
    ? formatSignedMoneyAndPercent(savingsGain, savingsGainPct)
    : "אין עדיין נתונים";
  setTrendClass($("stat-savings-gain"), savingsGain);
  $("stat-day-change").textContent = `${dayChange >= 0 ? "+" : ""}${formatMoney(dayChange)}`;
  setTrendClass($("stat-day-change"), dayChange);
  $("cash-share").textContent = tradingAccountValue > 0 ? `${(cash / tradingAccountValue * 100).toFixed(1)}% מחשבון המסחר` : "ללא יתרה";
  $("cash-live-value").textContent = formatMoney(cash);

  const updatedAt = currentUserData.last_valuation_at;
  $("updated-at").textContent = updatedAt?.toDate
    ? `עודכן ${updatedAt.toDate().toLocaleString("he-IL")}`
    : "ממתין לעדכון מחירים מהבוט";

  const holdingValues = Object.values(holdings).map((h) => numberOrZero(h.market_value));
  const maxValue = Math.max(0, ...holdingValues);
  const concentration = securitiesValue > 0 ? maxValue / securitiesValue * 100 : 0;
  $("concentration-badge").textContent = concentration ? `החזקה מובילה ${concentration.toFixed(0)}%` : "אין נתונים";
  $("concentration-badge").classList.toggle("risk", concentration >= 40);
  $("portfolio-insight").textContent = buildInsight(holdings, cash, accountValue, concentration, valuation, currentFinancialAssets);

  renderChart(holdings, cash, currentFinancialAssets);
  renderBenchmarkComparison(valuation?.benchmark);
  renderHoldings(holdings);
  populatePortfolioAnalysis(holdings);
  renderFinancialAssets(currentFinancialAssets);
  renderGoal(accountValue);
  populateTaxTickers(holdings);
  renderPricingWarning(valuation);
  populateProfileForm();
  if (currentUserData.last_analysis && !$("analysis-result").dataset.busy) {
    renderAnalysis(currentUserData.last_analysis);
  }
  const deepTimestamp = currentUserData.last_deep_analysis_at;
  const deepVersion = deepTimestamp?.toMillis
    ? String(deepTimestamp.toMillis())
    : JSON.stringify(currentUserData.last_deep_analysis || null);
  if (currentUserData.last_deep_analysis && !$("ai-answer").dataset.busy && deepVersion !== lastRenderedDeepAnalysisVersion) {
    renderDeepAnalysis(currentUserData.last_deep_analysis);
    lastRenderedDeepAnalysisVersion = deepVersion;
  }
}

function buildInsight(holdings, cash, accountValue, concentration, valuation, financialAssets = {}) {
  const count = Object.keys(holdings).length;
  const savingsCount = Object.keys(financialAssets).length;
  if (!count && !savingsCount) return cash > 0 ? "החשבון מחזיק מזומן בלבד. אפשר להתחיל בניתוח נכס לפני קנייה." : "החשבון עדיין ריק. אפשר להוסיף קנייה או מכשיר פיננסי מהאתר.";
  const notes = [];
  if (count) notes.push(`${count} החזקות סחירות`);
  if (savingsCount) notes.push(`${savingsCount} קופות/חסכונות`);
  if (accountValue > 0 && cash / accountValue >= 0.2) notes.push("כרית מזומן משמעותית");
  if (concentration >= 40) notes.push("ריכוזיות גבוהה בהחזקה אחת");
  if (valuation && valuation.pricing_complete === false) notes.push("חלק מהמחירים חסרים");
  return notes.join(" · ");
}

function renderChart(holdings, cash, financialAssets = {}) {
  const grouped = new Map();
  const addValue = (label, value) => {
    if (!(numberOrZero(value) > 0)) return;
    grouped.set(label, numberOrZero(grouped.get(label)) + numberOrZero(value));
  };
  for (const [ticker, holding] of Object.entries(holdings)) {
    if (numberOrZero(holding.market_value) > 0) {
      const label = allocationMode === "currency"
        ? (holding.quote_currency || holding.account_currency || "מטבע לא ידוע")
        : allocationMode === "sector"
          ? (holding.sector || holding.category || "סקטור לא ידוע")
          : allocationMode === "market"
            ? (holding.country || holding.market || holding.exchange || "שוק לא ידוע")
            : displayLabel(ticker, holding);
      addValue(label, holding.market_value);
    }
  }
  if (cash > 0) {
    const cashLabel = allocationMode === "currency" ? (currentProfile.base_currency || "ILS") : "מזומן פנוי";
    addValue(cashLabel, cash);
  }
  for (const asset of Object.values(financialAssets)) {
    const value = numberOrZero(asset.estimated_balance ?? asset.reported_balance);
    if (value > 0) {
      const label = allocationMode === "currency" ? "ILS"
        : allocationMode === "sector" ? "חיסכון ארוך טווח"
          : allocationMode === "market" ? "ישראל — קופת גמל"
            : (asset.name || "קופת גמל");
      addValue(label, value);
    }
  }
  const labels = [...grouped.keys()];
  const values = [...grouped.values()];
  if (chartInstance) chartInstance.destroy();
  const hasData = values.length > 0;
  $("pie-chart-frame").classList.toggle("hidden", !hasData);
  $("empty-msg").classList.toggle("hidden", hasData);
  if (!hasData) return;
  chartInstance = new Chart($("pie-chart"), {
    type: "doughnut",
    data: { labels, datasets: [{
      data: values,
      backgroundColor: ["#46d7a7", "#5da9ff", "#9b8cff", "#f6c760", "#ff7d90", "#54c7ec", "#c9f27b", "#60758d"],
      borderColor: "#0b1726", borderWidth: 4, hoverOffset: 8,
    }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "67%",
      plugins: { legend: { position: "bottom", labels: { color: "#b9c7d7", boxWidth: 10, usePointStyle: true, padding: 18 } } },
    },
  });
}

const BENCHMARK_PERIOD_LABELS = [
  ["day_change_pct", "יום"],
  ["week_change_pct", "שבוע"],
  ["month_change_pct", "חודש"],
  ["year_change_pct", "שנה"],
];

const BENCHMARK_INDEX_ORDER = ["sp500", "nasdaq100", "ta125"];
const BENCHMARK_INDEX_COLORS = { sp500: "#5da9ff", nasdaq100: "#9b8cff", ta125: "#f6c760" };

function renderBenchmarkComparison(benchmark) {
  const portfolio = benchmark?.portfolio || {};
  const indices = benchmark?.indices || {};
  const periods = BENCHMARK_PERIOD_LABELS.filter(([key]) => portfolio[key] != null);
  if (benchmarkChartInstance) benchmarkChartInstance.destroy();
  $("benchmark-chart-frame").classList.toggle("hidden", !periods.length);
  $("benchmark-empty").classList.toggle("hidden", periods.length > 0);

  const referencePeriod = ["year_change_pct", "month_change_pct", "week_change_pct", "day_change_pct"]
    .find((key) => portfolio[key] != null) || null;
  const referenceIndexValues = BENCHMARK_INDEX_ORDER
    .map((key) => indices[key]?.[referencePeriod])
    .filter((value) => value != null);
  const referenceIndexAvg = referenceIndexValues.length
    ? referenceIndexValues.reduce((sum, value) => sum + value, 0) / referenceIndexValues.length
    : null;
  $("benchmark-badge").textContent = referencePeriod && referenceIndexAvg != null
    ? (numberOrZero(portfolio[referencePeriod]) >= referenceIndexAvg ? "התיק מוביל" : "המדדים מובילים")
    : "—";
  if (!periods.length) return;

  const datasets = [
    { label: "התיק שלי", data: periods.map(([key]) => Number(portfolio[key].toFixed(2))), backgroundColor: "#46d7a7", borderRadius: 6 },
  ];
  for (const key of BENCHMARK_INDEX_ORDER) {
    const index = indices[key];
    if (!index || !index.label) continue;
    datasets.push({
      label: index.label,
      data: periods.map(([period]) => (index[period] != null ? Number(index[period].toFixed(2)) : null)),
      backgroundColor: BENCHMARK_INDEX_COLORS[key] || "#60758d",
      borderRadius: 6,
    });
  }

  benchmarkChartInstance = new Chart($("benchmark-chart"), {
    type: "bar",
    data: { labels: periods.map(([, label]) => label), datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { ticks: { color: "#b9c7d7", autoSkip: false, maxRotation: 0 }, grid: { display: false } },
        y: { ticks: { color: "#b9c7d7", callback: (value) => `${value}%` }, grid: { color: "#1c2c3d" } },
      },
      plugins: {
        legend: { position: "bottom", labels: { color: "#b9c7d7", boxWidth: 10, usePointStyle: true, padding: 18 } },
        tooltip: { callbacks: { label: (item) => `${item.dataset.label}: ${item.parsed.y == null ? "אין נתון" : `${item.parsed.y >= 0 ? "+" : ""}${item.parsed.y}%`}` } },
      },
    },
  });
}

document.querySelectorAll(".allocation-tab").forEach((button) => {
  button.addEventListener("click", () => {
    allocationMode = button.dataset.mode || "asset";
    document.querySelectorAll(".allocation-tab").forEach((item) => {
      item.classList.toggle("active", item === button);
      item.classList.toggle("ghost", item !== button);
    });
    renderChart(currentValuation?.holdings || {}, numberOrZero(currentUserData.cash_balance ?? currentValuation?.cash_balance), currentFinancialAssets);
  });
});

function populatePortfolioAnalysis(holdings) {
  const select = $("portfolio-analysis-ticker");
  const previous = select.value;
  select.innerHTML = "";
  for (const [ticker, holding] of Object.entries(holdings)) {
    const option = document.createElement("option");
    option.value = ticker;
    option.textContent = displayLabel(ticker, holding);
    select.append(option);
  }
  if (previous && holdings[previous]) select.value = previous;
  $("portfolio-analysis-btn").disabled = !select.value;
}

$("portfolio-analysis-btn").addEventListener("click", () => {
  const symbol = $("portfolio-analysis-ticker").value;
  if (!symbol) return;
  $("analysis-symbol").value = symbol;
  $("analysis-btn").click();
  $("analysis-result").scrollIntoView({ behavior: "smooth", block: "center" });
});

function renderHoldings(holdings) {
  const host = $("holdings-list");
  host.innerHTML = "";
  const entries = Object.entries(holdings).sort((a, b) => numberOrZero(b[1].market_value) - numberOrZero(a[1].market_value));
  $("holdings-count").textContent = String(entries.length);
  if (!entries.length) {
    host.append(emptyNote("אין החזקות להצגה."));
    return;
  }
  const header = document.createElement("div");
  header.className = "holding-row holding-header";
  header.innerHTML = "<span>נייר</span><span>שווי</span><span>רווח/הפסד</span><span>שינוי</span>";
  host.append(header);
  for (const [ticker, holding] of entries) {
    const row = document.createElement("div");
    row.className = "holding-row";
    const gain = holding.gain_loss;
    const gainPct = holding.gain_loss_pct ?? (
      gain != null && numberOrZero(holding.cost_basis) > 0
        ? Number(gain) / numberOrZero(holding.cost_basis) * 100
        : null
    );
    const changes = [
      ["יום", holding.day_change_pct], ["שבוע", holding.week_change_pct],
      ["חודש", holding.month_change_pct ?? holding.period_change_pct], ["שנה", holding.year_change_pct],
    ].filter(([, value]) => value != null);
    const primaryChange = changes[0];
    const extraChanges = changes.slice(1).map(([label, value]) =>
      `<small class="period-change ${trendClass(value)}">${escapeHtml(label)}: ${value >= 0 ? "+" : ""}${Number(value).toFixed(1)}%</small>`
    ).join("");
    const fetchedAt = formatIsoTime(holding.price_fetched_at);
    const sourceText = holding.price_source ? `<small class="price-source">מקור: ${escapeHtml(holding.price_source)}${fetchedAt ? ` · ${escapeHtml(fetchedAt)}` : ""}</small>` : "";
    const gainPeriod = holding.buy_date ? `מאז ${escapeHtml(holding.buy_date)}` : "מאז מחיר הבסיס";
    const fxImpact = holding.fx_gain_loss == null ? "" : `<small class="fx-impact">השפעת מט״ח: ${holding.fx_gain_loss >= 0 ? "+" : ""}${formatMoney(holding.fx_gain_loss)} · שער קנייה ${Number(holding.buy_fx_rate).toFixed(4)} מול נוכחי ${Number(holding.current_fx_rate).toFixed(4)}</small>`;
    const quoteCurrency = String(holding.quote_currency || "").toUpperCase();
    const originalAverage = ["USD", "EUR"].includes(quoteCurrency)
      ? `ממוצע ${formatQuoteMoney(holding.buy_price, quoteCurrency)} · `
      : "";
    row.innerHTML = `
      <span class="asset-cell"><strong>${displayLabelHtml(ticker, holding)}</strong><small>${formatQuantity(holding.quantity)} יח' · ${originalAverage}בסיס מדויק ${formatMoney(holding.exact_unit_cost_account_currency ?? holding.buy_price_account_currency ?? holding.buy_price)}</small></span>
      <span class="holding-value" data-label="שווי">${holding.market_value == null ? "לא זמין" : formatMoney(holding.market_value)}</span>
      <span class="holding-value ${trendClass(gain)}" data-label="רווח/הפסד">${gain == null ? "—" : `${gain >= 0 ? "+" : ""}${formatMoney(gain)}<small class="holding-return-pct">${gainPct == null ? "" : `${gainPct >= 0 ? "+" : ""}${Number(gainPct).toFixed(2)}%`}</small>${fxImpact}<small class="gain-period">${gainPeriod}</small>`}</span>
      <span class="holding-value ${trendClass(primaryChange?.[1])}" data-label="שינוי">${primaryChange ? `${escapeHtml(primaryChange[0])}: ${primaryChange[1] >= 0 ? "+" : ""}${Number(primaryChange[1]).toFixed(1)}%` : "—"}${extraChanges}${sourceText}</span>`;
    host.append(row);
  }
}

function renderPricingWarning(valuation) {
  const el = $("pricing-warning");
  const missing = valuation?.unpriced_tickers || [];
  el.classList.toggle("hidden", missing.length === 0);
  const holdings = valuation?.holdings || {};
  const names = missing.map((ticker) => displayLabel(ticker, holdings[ticker] || {}));
  el.textContent = names.length ? `אין מחיר עדכני עבור ${names.join(", ")}; הם אינם מחושבים בתשואה.` : "";
}

function renderFinancialAssets(assets) {
  const host = $("financial-assets-list");
  const entries = Object.entries(assets);
  const total = entries.reduce((sum, [, asset]) => sum + numberOrZero(asset.estimated_balance ?? asset.reported_balance), 0);
  $("savings-total-badge").textContent = formatMoney(total);
  host.innerHTML = "";
  if (!entries.length) {
    host.append(emptyNote("עדיין לא נוספו קופות או חסכונות."));
    return;
  }
  for (const [id, asset] of entries) {
    const estimated = numberOrZero(asset.estimated_balance ?? asset.reported_balance);
    const reported = numberOrZero(asset.reported_balance);
    const gain = asset.estimated_gain_loss;
    const gainPct = asset.estimated_gain_loss_pct;
    const card = document.createElement("article");
    card.className = "financial-asset-card";
    card.innerHTML = `
      <div class="asset-card-heading"><div><strong>${escapeHtml(asset.name || "מכשיר פיננסי")}</strong><small>${escapeHtml(asset.provider || "")} · קופה ${escapeHtml(asset.track_id || "")}</small></div><span>${formatMoney(estimated)}</span></div>
      <div class="asset-metrics">
        <span><small>צבירה שהוזנה</small><strong>${formatMoney(reported)}</strong><em>נכון ל־${escapeHtml(asset.balance_as_of || "—")}</em></span>
        <span><small>אומדן מעודכן</small><strong>${formatMoney(estimated)}</strong><em>דיווח ציבורי ${escapeHtml(asset.latest_report_period || "טרם נטען")}</em></span>
        <span><small>חודש דיווח אחרון</small><strong class="${trendClass(asset.monthly_return_pct)}">${formatPct(asset.monthly_return_pct)}</strong><em>לא תשואה יומית</em></span>
        <span><small>12 חודשי דיווח</small><strong class="${trendClass(asset.return_12m_pct)}">${formatPct(asset.return_12m_pct)}</strong><em>תשואה ציבורית למסלול</em></span>
      </div>
      ${gain == null ? `<p class="method-note">כדי לחשב רווח/הפסד אישי, הזן גם את סך ההפקדות.</p>` : `<p class="asset-gain ${trendClass(gain)}">רווח/הפסד משוער מאז יתרת הבסיס וההפקדות שהוזנו: ${gain >= 0 ? "+" : ""}${formatMoney(gain)} · ${gainPct >= 0 ? "+" : ""}${Number(gainPct || 0).toFixed(2)}%</p>`}
      <p class="method-note">אומדן לפי תשואות חודשיות שפורסמו; אינו יתרה חיה ואינו כולל בהכרח את דמי הניהול האישיים.</p>
      <div class="button-row compact-actions"><button class="secondary asset-update" data-id="${escapeHtml(id)}">עריכת הקופה</button><button class="ghost asset-delete" data-id="${escapeHtml(id)}">הסר</button></div>`;
    host.append(card);
  }
  const shouldShowEditor = !entries.length || assetAddingNew || Boolean(editingAssetId);
  $("asset-editor").classList.toggle("hidden", !shouldShowEditor);
  $("asset-new-btn").classList.toggle("hidden", !entries.length || shouldShowEditor);
  $("asset-cancel-btn").classList.toggle("hidden", !entries.length || (!assetAddingNew && !editingAssetId));
}

function renderGoal(currentValue) {
  const goal = currentUserData.financial_goal || {};
  const target = numberOrZero(goal.target_amount);
  const percent = target > 0 ? Math.min(100, currentValue / target * 100) : 0;
  $("goal-progress").style.width = `${percent}%`;
  $("goal-percent").textContent = target > 0 ? `${percent.toFixed(1)}%` : "טרם הוגדר";
  $("goal-current").textContent = target > 0
    ? `${formatMoney(currentValue)} מתוך ${formatMoney(target)}`
    : `שווי נוכחי: ${formatMoney(currentValue)}`;
  $("goal-remaining").textContent = target > 0
    ? currentValue >= target ? "היעד הושג 🎉" : `נותרו ${formatMoney(target - currentValue)} ליעד`
    : "הגדר יעד כדי להתחיל לעקוב";
  if (document.activeElement !== $("goal-name")) $("goal-name").value = goal.name || "היעד הפיננסי שלי";
  if (document.activeElement !== $("goal-target")) $("goal-target").value = target || "";
  const showEditor = !target || goalEditing;
  $("goal-editor").classList.toggle("hidden", !showEditor);
  $("goal-edit-btn").classList.toggle("hidden", !target || showEditor);
}

$("goal-edit-btn").addEventListener("click", () => {
  goalEditing = true;
  renderGoal(numberOrZero(currentValuation?.total_financial_value ?? currentValuation?.account_total_value));
  $("goal-name").focus();
});

$("goal-save-btn").addEventListener("click", async () => {
  const target = Number($("goal-target").value);
  const name = $("goal-name").value.trim() || "היעד הפיננסי שלי";
  if (!(target > 0)) {
    showMessage("goal-message", "נא להזין סכום יעד גדול מאפס.", false);
    return;
  }
  try {
    await updateDoc(doc(db, "users", currentTelegramId), {
      financial_goal: { name, target_amount: target, updated_at: serverTimestamp() },
    });
    goalEditing = false;
    showMessage("goal-message", "היעד נשמר ומד ההתקדמות עודכן.", true);
  } catch (error) {
    showMessage("goal-message", "לא ניתן היה לשמור את היעד.", false);
    console.error(error);
  }
});

$("asset-date").value = new Date().toISOString().slice(0, 10);

function resetAssetEditor() {
  editingAssetId = null;
  assetAddingNew = false;
  $("asset-track").disabled = false;
  $("asset-balance").value = "";
  $("asset-contributed").value = "";
  $("asset-monthly").value = "";
  $("asset-date").value = new Date().toISOString().slice(0, 10);
  $("asset-add-btn").textContent = "שמור מכשיר";
  renderFinancialAssets(currentFinancialAssets);
}

$("asset-new-btn").addEventListener("click", () => {
  editingAssetId = null;
  assetAddingNew = true;
  $("asset-track").disabled = false;
  $("asset-editor").classList.remove("hidden");
  $("asset-cancel-btn").classList.remove("hidden");
  $("asset-balance").focus();
});

$("asset-cancel-btn").addEventListener("click", resetAssetEditor);

$("asset-add-btn").addEventListener("click", async () => {
  const trackId = $("asset-track").value;
  const metadata = SAVINGS_TRACKS[trackId];
  const reportedBalance = Number($("asset-balance").value);
  const balanceAsOf = $("asset-date").value;
  const totalContributed = Number($("asset-contributed").value || 0);
  const monthlyContribution = Number($("asset-monthly").value || 0);
  if (!metadata || !(reportedBalance > 0) || !balanceAsOf || totalContributed < 0 || monthlyContribution < 0) {
    showMessage("asset-message", "בדוק את המסלול, הצבירה והתאריך.", false);
    return;
  }
  try {
    const wasEditing = Boolean(editingAssetId);
    const payload = {
      asset_type: "gemel_investment", provider: metadata.provider, track_id: trackId,
      name: metadata.name, reported_balance: reportedBalance, balance_as_of: balanceAsOf,
      total_contributed: totalContributed, monthly_contribution: monthlyContribution,
      auto_update: true,
    };
    const existingSameTrack = Object.values(currentFinancialAssets).find((asset) => asset.track_id === trackId);
    if (editingAssetId) {
      await updateDoc(doc(db, "users", currentTelegramId, "financial_assets", editingAssetId), {
        reported_balance: reportedBalance, balance_as_of: balanceAsOf,
        total_contributed: totalContributed, monthly_contribution: monthlyContribution,
        name: metadata.name,
      });
    } else if (existingSameTrack) {
      await updateDoc(doc(db, "users", currentTelegramId, "financial_assets", existingSameTrack.id), {
        reported_balance: reportedBalance, balance_as_of: balanceAsOf,
        total_contributed: totalContributed, monthly_contribution: monthlyContribution,
        name: metadata.name,
      });
    } else {
      await addDoc(collection(db, "users", currentTelegramId, "financial_assets"), {
        ...payload, created_at: serverTimestamp(),
      });
    }
    resetAssetEditor();
    showMessage("asset-message", existingSameTrack || wasEditing ? "הקופה עודכנה. מרענן תשואות…" : "המכשיר נוסף. מרענן את נתוני המסלול…", true);
    requestSavingsRefresh();
  } catch (error) {
    showMessage("asset-message", "לא ניתן היה להוסיף את המכשיר.", false);
    console.error(error);
  }
});

$("assets-refresh-btn").addEventListener("click", requestSavingsRefresh);

$("financial-assets-list").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-id]");
  if (!button) return;
  const asset = currentFinancialAssets[button.dataset.id];
  if (!asset) return;
  try {
    if (button.classList.contains("asset-delete")) {
      if (!window.confirm(`להסיר את ${asset.name}?`)) return;
      await deleteDoc(doc(db, "users", currentTelegramId, "financial_assets", button.dataset.id));
      showMessage("asset-message", "המכשיר הוסר.", true);
    } else {
      editingAssetId = button.dataset.id;
      assetAddingNew = false;
      $("asset-track").value = asset.track_id;
      $("asset-track").disabled = true;
      $("asset-balance").value = asset.reported_balance || "";
      $("asset-date").value = asset.balance_as_of || new Date().toISOString().slice(0, 10);
      $("asset-contributed").value = asset.total_contributed || "";
      $("asset-monthly").value = asset.monthly_contribution || "";
      $("asset-add-btn").textContent = "שמור שינויים";
      $("asset-editor").classList.remove("hidden");
      $("asset-cancel-btn").classList.remove("hidden");
      $("asset-editor").scrollIntoView({ behavior: "smooth", block: "center" });
    }
  } catch (error) {
    showMessage("asset-message", "העדכון נכשל. בדוק את הסכום ונסה שוב.", false);
    console.error(error);
  }
});

async function requestSavingsRefresh() {
  if (!currentTelegramId) return;
  showMessage("asset-message", "הבוט מרענן את הדיווח החודשי…", true);
  try {
    const ref = await addDoc(collection(db, "users", currentTelegramId, "ai_requests"), {
      kind: "financial_assets", question: "רענן נתוני קופות וחסכונות",
      status: "pending", created_at: serverTimestamp(),
    });
    const unsubscribe = onSnapshot(ref, (snapshot) => {
      const data = snapshot.data();
      if (data?.status !== "answered") return;
      showMessage("asset-message", "נתוני הקופות רועננו.", true);
      unsubscribe();
    }, (error) => {
      showMessage("asset-message", "הרענון נכשל; היתרה שהזנת נשמרה.", false);
      console.error(error);
    });
  } catch (error) {
    showMessage("asset-message", "הרענון נכשל; היתרה שהזנת נשמרה.", false);
    console.error(error);
  }
}

function populateProfileForm() {
  $("profile-name").value = currentProfile.display_name || "";
  $("profile-risk").value = currentProfile.risk_profile;
  $("profile-horizon").value = currentProfile.investment_horizon;
  $("profile-goal").value = currentProfile.investment_goal || "";
  $("profile-currency").value = currentProfile.base_currency;
  const labels = { conservative: "שמרני", balanced: "מאוזן", aggressive: "אגרסיבי", short: "קצר", medium: "בינוני", long: "ארוך" };
  $("profile-summary").innerHTML = `<strong>${escapeHtml(currentProfile.display_name || "הפרופיל שלי")}</strong><span>סיכון: ${escapeHtml(labels[currentProfile.risk_profile] || currentProfile.risk_profile)} · טווח: ${escapeHtml(labels[currentProfile.investment_horizon] || currentProfile.investment_horizon)} · מטבע בסיס: ${escapeHtml(currentProfile.base_currency || "ILS")}</span><small>${escapeHtml(currentProfile.investment_goal || "לא הוגדרה מטרה אישית")}</small>`;
  const hasProfile = Boolean(currentUserData.profile);
  $("profile-editor").classList.toggle("hidden", hasProfile && !profileEditing);
  $("profile-summary").classList.toggle("hidden", !hasProfile || profileEditing);
  $("profile-edit-btn").classList.toggle("hidden", !hasProfile || profileEditing);
}

$("profile-edit-btn").addEventListener("click", () => {
  profileEditing = true;
  populateProfileForm();
  $("profile-name").focus();
});

$("profile-save-btn").addEventListener("click", async () => {
  if (!currentTelegramId) return;
  const profile = {
    display_name: $("profile-name").value.trim(),
    risk_profile: $("profile-risk").value,
    investment_horizon: $("profile-horizon").value,
    investment_goal: $("profile-goal").value.trim(),
    base_currency: $("profile-currency").value,
  };
  try {
    await updateDoc(doc(db, "users", currentTelegramId), { profile });
    profileEditing = false;
    showMessage("profile-message", "הפרופיל נשמר וההמלצות הבאות יותאמו אליו.", true);
  } catch (error) {
    showMessage("profile-message", "לא ניתן היה לשמור את הפרופיל.", false);
    console.error(error);
  }
});

async function changeCash(mode) {
  if (!currentTelegramId) return;
  const amount = Number($("cash-amount").value);
  if (!Number.isFinite(amount) || amount < 0 || (mode !== "set" && amount === 0)) {
    showMessage("cash-message", "נא להזין סכום תקין.", false);
    return;
  }
  try {
    const userRef = doc(db, "users", currentTelegramId);
    await runTransaction(db, async (transaction) => {
      const snapshot = await transaction.get(userRef);
      const current = numberOrZero(snapshot.data()?.cash_balance);
      const next = mode === "set" ? amount : mode === "deposit" ? current + amount : current - amount;
      if (next < 0) throw new Error("insufficient-cash");
      transaction.update(userRef, { cash_balance: next });
    });
    $("cash-amount").value = "";
    showMessage("cash-message", "יתרת המזומן עודכנה וסונכרנה.", true);
  } catch (error) {
    showMessage("cash-message", error.message === "insufficient-cash" ? "אין מספיק מזומן למשיכה." : "לא ניתן היה לעדכן את היתרה.", false);
    console.error(error);
  }
}

$("cash-deposit-btn").addEventListener("click", () => changeCash("deposit"));
$("cash-withdraw-btn").addEventListener("click", () => changeCash("withdraw"));
$("cash-set-btn").addEventListener("click", () => changeCash("set"));

const AGOROT_SECURITY_CODES = new Set(["1215771", "1144401", "5112628", "5141189"]);

$("web-buy-btn").addEventListener("click", async () => {
  const ticker = $("web-buy-ticker").value.trim().toUpperCase();
  const name = $("web-buy-name").value.trim();
  const quantity = Number($("web-buy-qty").value);
  const displayedPrice = Number($("web-buy-price").value);
  const buyFxRate = $("web-buy-fx").value ? Number($("web-buy-fx").value) : null;
  if (!ticker || !(quantity > 0) || !(displayedPrice > 0) || (buyFxRate != null && !(buyFxRate > 0))) {
    showMessage("web-buy-message", "נא להזין טיקר, כמות ומחיר תקינים.", false);
    return;
  }
  const buyPrice = AGOROT_SECURITY_CODES.has(ticker) ? displayedPrice * 100 : displayedPrice;
  $("web-buy-btn").disabled = true;
  try {
    const data = await submitPortfolioRequest({
      type: "buy", ticker, name, quantity, buy_price: buyPrice,
      ...(buyFxRate ? { buy_fx_rate: buyFxRate } : {}),
    });
    showMessage("web-buy-message", data.message || "הקנייה נוספה.", true);
    $("web-buy-ticker").value = "";
    $("web-buy-name").value = "";
    $("web-buy-qty").value = "";
    $("web-buy-price").value = "";
    $("web-buy-fx").value = "";
  } catch (error) {
    showMessage("web-buy-message", error.message || "הקנייה לא בוצעה.", false);
  } finally {
    $("web-buy-btn").disabled = false;
  }
});

$("portfolio-file").addEventListener("change", async (event) => {
  pendingImportHoldings = null;
  pendingImportPlan = null;
  $("import-preview").classList.add("hidden");
  $("import-confirm-btn").classList.add("hidden");
  const file = event.target.files?.[0];
  if (!file) return;
  const isImage = /\.(png|jpe?g)$/i.test(file.name) || /^image\/(png|jpeg)$/.test(file.type);
  const isExcel = /\.(xlsx|xls)$/i.test(file.name);
  if ((!isImage && !isExcel) || file.size > 8 * 1024 * 1024) {
    showMessage("web-import-message", "יש לבחור Excel או תמונת PNG/JPG עד 8MB.", false);
    return;
  }
  try {
    if (isImage) {
      showMessage("web-import-message", "התמונה נשלחת לזיהוי מאובטח. זה עשוי לקחת עד דקה…", true);
      const imageDataUrl = await compressPortfolioImage(file);
      const parsed = await submitPortfolioRequest({ type: "image_parse", image_data_url: imageDataUrl });
      pendingImportHoldings = consolidateImportHoldings(parsed.result?.holdings || []);
    } else {
      const workbook = XLSX.read(await file.arrayBuffer(), { type: "array" });
      const aiSheetCandidates = [];
      let locallyParsed = [];
      for (const sheetName of workbook.SheetNames) {
        const rows = XLSX.utils.sheet_to_json(workbook.Sheets[sheetName], { header: 1, defval: "", raw: true });
        const nonEmptyRows = rows.filter((row) => Array.isArray(row) && row.some((value) => String(value ?? "").trim()));
        if (nonEmptyRows.length) {
          aiSheetCandidates.push({ sheetName, rows: nonEmptyRows, score: scorePortfolioSheet(nonEmptyRows) });
        }
        try {
          locallyParsed = parsePortfolioRows(rows);
        } catch (_) {
          locallyParsed = [];
        }
        if (locallyParsed.length) break;
      }
      if (locallyParsed.length) {
        pendingImportHoldings = consolidateImportHoldings(locallyParsed);
      } else {
        const bestSheet = aiSheetCandidates.sort((left, right) => right.score - left.score || right.rows.length - left.rows.length)[0];
        if (!bestSheet) throw new Error("לא נמצאה טבלת החזקות בגיליונות הקובץ.");
        showMessage("web-import-message", "מבנה העמודות לא מוכר. ה-AI מזהה רק את מיקום העמודות…", true);
        const compactRows = bestSheet.rows.slice(0, 250).map((row) => row.slice(0, 60));
        const rowsJson = JSON.stringify(compactRows);
        if (rowsJson.length >= 250000) throw new Error("הגיליון רחב מדי. השאר את אזור טבלת ההחזקות ונסה שוב.");
        const parsed = await submitPortfolioRequest({ type: "excel_parse", rows_json: rowsJson });
        pendingImportHoldings = consolidateImportHoldings(parsed.result?.holdings || []);
      }
    }
    if (!pendingImportHoldings.length) throw new Error("לא נמצאו שורות תקינות.");
    if (pendingImportHoldings.length > 200) throw new Error("ניתן לייבא עד 200 החזקות.");
    pendingImportPlan = buildImportPlan(pendingImportHoldings, currentValuation?.holdings || {});
    renderImportPreview(pendingImportHoldings, pendingImportPlan);
    $("import-confirm-btn").classList.remove("hidden");
    showMessage("web-import-message", `זוהו ${pendingImportHoldings.length} החזקות. בדוק את התצוגה המקדימה ואשר.`, true);
  } catch (error) {
    pendingImportHoldings = null;
    const rawMessage = String(error?.message || "שגיאה לא ידועה");
    const schemaMessage = /נדרשות עמודות|לא זוהתה שורת כותרות|טיקר\/מספר נייר/.test(rawMessage)
      ? "לא הצלחתי לזהות את טבלת ההחזקות גם לאחר זיהוי חכם. ודא שבקובץ מופיעים נייר, כמות ומחיר בסיס או עלות כוללת."
      : rawMessage;
    showMessage("web-import-message", `לא ניתן לקרוא את הקובץ: ${schemaMessage}`, false);
  }
});

async function compressPortfolioImage(file) {
  const bitmap = await createImageBitmap(file);
  const maxSide = 1400;
  const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  for (const quality of [0.78, 0.68, 0.58, 0.48]) {
    const dataUrl = canvas.toDataURL("image/jpeg", quality);
    if (dataUrl.length < 700000) return dataUrl;
  }
  throw new Error("התמונה עדיין גדולה מדי לאחר כיווץ. חתוך לאזור ההחזקות ונסה שוב.");
}

$("import-confirm-btn").addEventListener("click", async () => {
  if (!pendingImportHoldings?.length) return;
  const counts = pendingImportPlan?.counts || {};
  if (!window.confirm(`לסנכרן את התיק? ${counts.added || 0} יתווספו, ${counts.updated || 0} יעודכנו ו־${counts.removed || 0} יוסרו.`)) return;
  const payloadJson = JSON.stringify(pendingImportHoldings);
  if (payloadJson.length >= 150000) {
    showMessage("web-import-message", "הייבוא גדול מדי. צמצם את מספר השורות.", false);
    return;
  }
  $("import-confirm-btn").disabled = true;
  try {
    const data = await submitPortfolioRequest({ type: "import", payload_json: payloadJson });
    const summary = data.result?.summary;
    const summaryText = summary
      ? `נוספו ${summary.added}, עודכנו ${summary.updated}, נמכרו/הוסרו ${summary.removed}, ללא שינוי ${summary.unchanged}.`
      : "";
    showMessage("web-import-message", `${data.message || "הסנכרון הושלם."} ${summaryText}`.trim(), true);
    pendingImportHoldings = null;
    pendingImportPlan = null;
    $("import-preview").classList.add("hidden");
    $("import-confirm-btn").classList.add("hidden");
    $("portfolio-file").value = "";
  } catch (error) {
    showMessage("web-import-message", error.message || "הייבוא לא בוצע.", false);
  } finally {
    $("import-confirm-btn").disabled = false;
  }
});

function parsePortfolioRows(rows) {
  const normalized = (value) => String(value ?? "").trim().toLowerCase().replace(/[\s_.\-\/]+/g, " ");
  const number = (value) => {
    if (typeof value === "number") return value;
    const cleaned = String(value ?? "").replace(/[₪$€,%\s]/g, "").replace(/,/g, "");
    const parsed = Number(cleaned);
    return Number.isFinite(parsed) ? parsed : NaN;
  };
  let headerIndex = -1;
  let tickerCol = -1, nameCol = -1, quantityCol = -1, unitPriceCol = -1, totalCostCol = -1;
  for (let index = 0; index < Math.min(rows.length, 80); index += 1) {
    const headers = rows[index].map(normalized);
    const find = (names) => headers.findIndex((header) => names.includes(header));
    const candidateTicker = find(["טיקר", "סימול", "מספר נייר", "מספר ני ע", "מספר ניע", "קוד נייר", "נייר", "ticker", "symbol", "security number", "security code"]);
    const candidateQuantity = find(["כמות", "יחידות", "פוזיציה", "יתרה", "כמות מוחזקת", "quantity", "qty", "units", "units held", "position"]);
    const candidatePrice = find(["מחיר קנייה ממוצע", "מחיר קניה ממוצע", "שער עלות", "מחיר עלות", "מחיר בסיס", "מחיר קנייה", "מחיר קניה", "avg price", "average price", "unit cost", "average cost", "buy price"]);
    const candidateTotal = find(["עלות כוללת", "סך עלות", "שווי עלות", "בסיס עלות", "עלות", "total cost", "cost basis", "position cost", "cost"]);
    if (candidateTicker < 0 || candidateQuantity < 0 || (candidatePrice < 0 && candidateTotal < 0)) continue;
    headerIndex = index;
    tickerCol = candidateTicker;
    quantityCol = candidateQuantity;
    unitPriceCol = candidatePrice;
    totalCostCol = candidateTotal;
    nameCol = find(["שם נייר", "שם הנייר", "שם", "תיאור", "תאור", "name", "security name", "description"]);
    break;
  }
  if (headerIndex < 0) throw new Error("לא זוהתה שורת כותרות מקומית.");
  if (tickerCol < 0 || quantityCol < 0 || (unitPriceCol < 0 && totalCostCol < 0)) {
    throw new Error("נדרשות עמודות טיקר/מספר נייר, כמות ומחיר בסיס או עלות כוללת.");
  }
  const holdings = [];
  for (const row of rows.slice(headerIndex + 1)) {
    const ticker = String(row[tickerCol] ?? "").trim().replace(/\.0$/, "").toUpperCase();
    const quantity = number(row[quantityCol]);
    const reportedTotalCost = totalCostCol >= 0 ? number(row[totalCostCol]) : NaN;
    let buyPrice = unitPriceCol >= 0 ? number(row[unitPriceCol]) : NaN;
    let derivedFromTotalCost = false;
    if (!(buyPrice > 0) && totalCostCol >= 0 && quantity > 0) {
      buyPrice = reportedTotalCost / quantity;
      derivedFromTotalCost = true;
    }
    if (derivedFromTotalCost && AGOROT_SECURITY_CODES.has(ticker)) buyPrice *= 100;
    if (!ticker || !(quantity > 0) || !(buyPrice > 0)) continue;
    holdings.push({
      ticker,
      name: nameCol >= 0 ? String(row[nameCol] ?? "").trim().slice(0, 120) : "",
      quantity,
      buy_price: buyPrice,
      ...(reportedTotalCost > 0 ? { reported_total_cost: reportedTotalCost } : {}),
    });
  }
  return holdings;
}

function scorePortfolioSheet(rows) {
  const textRows = rows.slice(0, 80).map((row) =>
    row.map((value) => String(value ?? "").trim().toLowerCase().replace(/[\s_.\-\/]+/g, " ")),
  );
  let bestHeaderScore = 0;
  for (const row of textRows) {
    const joined = ` ${row.join(" | ")} `;
    const hasTicker = /(טיקר|סימול|מספר נייר|מספר ני ע|ticker|symbol|security (number|code)|מוצר פיננסי)/.test(joined);
    const hasQuantity = /(כמות|יחידות|פוזיציה|יתרה|quantity|qty|units|position)/.test(joined);
    const hasCost = /(מחיר בסיס|מחיר ממוצע|בסיס עלות|עלות כוללת|שווי עלות|average price|avg price|cost basis|total cost)/.test(joined);
    bestHeaderScore = Math.max(bestHeaderScore, Number(hasTicker) * 12 + Number(hasQuantity) * 10 + Number(hasCost) * 10);
  }
  const populatedRows = rows.slice(0, 250).filter((row) => row.filter((value) => String(value ?? "").trim()).length >= 3).length;
  return bestHeaderScore + Math.min(populatedRows, 40) / 10;
}

function consolidateImportHoldings(holdings) {
  const consolidated = new Map();
  for (const item of holdings) {
    const previous = consolidated.get(item.ticker);
    if (!previous) {
      consolidated.set(item.ticker, { ...item });
      continue;
    }
    const totalQuantity = previous.quantity + item.quantity;
    previous.buy_price = (previous.quantity * previous.buy_price + item.quantity * item.buy_price) / totalQuantity;
    previous.quantity = totalQuantity;
    if (previous.reported_total_cost != null && item.reported_total_cost != null) {
      previous.reported_total_cost += item.reported_total_cost;
    } else {
      delete previous.reported_total_cost;
    }
    if (!previous.name && item.name) previous.name = item.name;
  }
  return [...consolidated.values()];
}

function buildImportPlan(holdings, existingHoldings) {
  const incoming = new Map(holdings.map((item) => [item.ticker, item]));
  const added = [], updated = [], removed = [], unchanged = [];
  for (const item of holdings) {
    const previous = existingHoldings[item.ticker];
    if (!previous) {
      added.push(item);
      continue;
    }
    const changed = Math.abs(numberOrZero(previous.quantity) - item.quantity) > 1e-8
      || Math.abs(numberOrZero(previous.buy_price) - item.buy_price) > 1e-8
      || Math.abs(numberOrZero(previous.reported_total_cost) - numberOrZero(item.reported_total_cost)) > 1e-8;
    (changed ? updated : unchanged).push({
      ...item,
      old_quantity: numberOrZero(previous.quantity),
      old_buy_price: numberOrZero(previous.buy_price),
      name: item.name || displayLabel(item.ticker, previous),
    });
  }
  for (const [ticker, previous] of Object.entries(existingHoldings)) {
    if (!incoming.has(ticker)) removed.push({ ticker, name: displayLabel(ticker, previous), old_quantity: numberOrZero(previous.quantity) });
  }
  return { added, updated, removed, unchanged, counts: { added: added.length, updated: updated.length, removed: removed.length, unchanged: unchanged.length } };
}

function importPriceShekels(item) {
  const price = AGOROT_SECURITY_CODES.has(item.ticker) ? item.buy_price / 100 : item.buy_price;
  return Number(price).toLocaleString("he-IL", { maximumFractionDigits: 4 });
}

function renderImportPreview(holdings, plan) {
  const host = $("import-preview");
  const previewRows = holdings.slice(0, 20).map((item) =>
    `<tr><td><bdi>${escapeHtml(item.name || item.ticker)}</bdi></td><td>${formatQuantity(item.quantity)}</td><td>${importPriceShekels(item)} ₪</td><td>${item.reported_total_cost > 0 ? `${formatQuantity(item.reported_total_cost)} ₪` : "—"}</td></tr>`
  ).join("");
  const names = (items) => items.slice(0, 8).map((item) => `<li><bdi>${escapeHtml(item.name || item.ticker)}</bdi></li>`).join("");
  host.innerHTML = `<strong>סנכרון חכם — ${holdings.length} החזקות בקובץ</strong>
    <div class="sync-counts"><span class="positive">➕ ${plan.counts.added} חדשות</span><span>🔄 ${plan.counts.updated} השתנו</span><span class="negative">➖ ${plan.counts.removed} לא מופיעות</span><span>✅ ${plan.counts.unchanged} ללא שינוי</span></div>
    ${plan.updated.length ? `<details><summary>ניירות שהשתנו</summary><ul>${names(plan.updated)}</ul></details>` : ""}
    ${plan.removed.length ? `<details open><summary>יימחקו מהתיק כנמכרו/הוסרו</summary><ul>${names(plan.removed)}</ul></details>` : ""}
    <div class="preview-scroll"><table><thead><tr><th>נייר</th><th>כמות</th><th>מחיר ממוצע</th><th>בסיס עלות מדווח</th></tr></thead><tbody>${previewRows}</tbody></table></div>
    ${holdings.length > 20 ? `<small>מוצגות 20 השורות הראשונות.</small>` : ""}<p class="method-note">האישור מסנכרן לתמונת המצב שבקובץ; קופות וחסכונות לא ייפגעו.</p>`;
  host.classList.remove("hidden");
}

function submitPortfolioRequest(payload) {
  return new Promise(async (resolve, reject) => {
    try {
      const ref = await addDoc(collection(db, "users", currentTelegramId, "portfolio_requests"), {
        ...payload, status: "pending", created_at: serverTimestamp(),
      });
      let timer;
      const unsubscribe = onSnapshot(ref, (snapshot) => {
        const data = snapshot.data();
        if (!data || !["completed", "rejected"].includes(data.status)) return;
        clearTimeout(timer);
        unsubscribe();
        if (data.status === "completed") resolve(data);
        else reject(new Error(data.message || "הפעולה נדחתה."));
      }, (error) => reject(error));
      timer = setTimeout(() => {
        unsubscribe();
        reject(new Error("הבוט לא סיים את הפעולה בזמן. בדוק שהוא פועל ונסה שוב."));
      }, 180000);
    } catch (error) {
      reject(error);
    }
  });
}

$("analysis-btn").addEventListener("click", () => {
  const symbol = $("analysis-symbol").value.trim();
  if (!symbol) return;
  submitRequest({
    kind: "fundamental", symbol,
    question: `נתח פונדמנטלית את ${symbol} ובדוק אם הוא מתאים לי.`,
  }, "analysis");
});

$("ai-recommend-btn").addEventListener("click", () => submitRequest({
  kind: "question",
  question: "תן לי סקירה והמלצה קצרה ומאומתת על מצב התיק שלי כרגע.",
}, "ai"));

$("thinking-btn").addEventListener("click", () => submitRequest({
  kind: "thinking",
  question: "נתח לעומק את כל התיק שלי ותן תוכנית פעולה ברורה ומאומתת.",
}, "ai"));

$("ai-ask-btn").addEventListener("click", () => {
  const question = $("ai-question").value.trim();
  if (!question) return;
  submitRequest({ kind: "question", question }, "ai");
  $("ai-question").value = "";
});

async function submitRequest(request, target) {
  if (!currentTelegramId) return;
  if (requestUnsubscribe) requestUnsubscribe();
  const isAnalysis = target === "analysis";
  const isThinking = request.kind === "thinking";
  const host = isAnalysis ? $("analysis-result") : $("ai-answer");
  host.classList.remove("hidden");
  host.dataset.busy = "true";
  host.textContent = isAnalysis
    ? "אוסף דוחות, יחסים פיננסיים, ביצועים וחדשות. לאחר מכן מתבצעת בדיקת AI שנייה…"
    : isThinking
      ? "מצב חשיבה סורק כל החזקה, בודק את מבנה התיק ומבצע ביקורת AI שנייה. התהליך עשוי לקחת דקה…"
      : "מחשב נתוני תיק, מחפש חדשות ומאמת את התשובה…";
  toggleRequestButtons(true);
  try {
    const ref = await addDoc(collection(db, "users", currentTelegramId, "ai_requests"), {
      ...request, status: "pending", created_at: serverTimestamp(),
    });
    requestUnsubscribe = onSnapshot(ref, (snapshot) => {
      const data = snapshot.data();
      if (!data || data.status !== "answered") return;
      if (isAnalysis && data.analysis) renderAnalysis(data.analysis);
      else if (isThinking && data.analysis) renderDeepAnalysis(data.analysis);
      else host.textContent = data.answer || "לא התקבלה תשובה.";
      delete host.dataset.busy;
      toggleRequestButtons(false);
      if (requestUnsubscribe) requestUnsubscribe();
      requestUnsubscribe = null;
    });
  } catch (error) {
    const code = String(error?.code || "");
    host.textContent = code.includes("permission-denied")
      ? "הבקשה נחסמה בהרשאות. רענן את האתר והתחבר מחדש; אם הבעיה נמשכת, נסה שוב בעוד רגע."
      : code.includes("unavailable")
        ? "שירות הנתונים אינו זמין זמנית. בדוק את החיבור ונסה שוב."
        : "הבקשה נכשלה לפני שהגיעה לבוט. רענן את האתר ונסה שוב.";
    delete host.dataset.busy;
    toggleRequestButtons(false);
    console.error(error);
  }
}

function renderDeepAnalysis(result) {
  const host = $("ai-answer");
  host.classList.remove("hidden");
  const verdicts = {
    strong: "🟢 תיק חזק יחסית",
    healthy_but_watch: "🟡 תיק בריא, עם נקודות למעקב",
    needs_changes: "🟠 נדרשים שינויים",
    high_risk: "🔴 רמת סיכון גבוהה",
  };
  const list = (items, ordered = false) => {
    if (!Array.isArray(items) || !items.length) return "";
    const tag = ordered ? "ol" : "ul";
    return `<${tag}>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</${tag}>`;
  };
  const holdingActions = Array.isArray(result.holding_actions) ? result.holding_actions.map((item) =>
    `<li><strong><bdi>${escapeHtml(item.name || item.symbol || "נייר")}</bdi></strong> — ${escapeHtml(item.reason || item.stance || "מעקב")}</li>`
  ).join("") : "";
  host.innerHTML = `
    <div class="analysis-top"><div><p class="eyebrow">Deep portfolio thinking</p><h3>${escapeHtml(verdicts[result.overall_verdict] || "ניתוח תיק מלא")}</h3></div><span class="verified-badge">✓ ביטחון ${Number(result.confidence || 0)}%</span></div>
    <p class="analysis-summary">${escapeHtml(result.executive_summary || "")}</p>
    <h4>סיכונים מרכזיים</h4>${list(result.portfolio_risks)}
    <h4>המלצה לכל החזקה</h4>${holdingActions ? `<ul>${holdingActions}</ul>` : "<p>אין נתונים להצגה.</p>"}
    <h4>פעולות לפי סדר עדיפות</h4>${list(result.allocation_actions, true)}
    <h4>תוכנית מזומן</h4><p>${escapeHtml(result.cash_plan || "")}</p>
    <h4>הצעדים הבאים</h4>${list(result.next_steps, true)}
    <p class="verified-note">הניתוח נבדק מחדש מול הנתונים במעבר AI שני. הערכה לימודית בלבד, לא ייעוץ השקעות.</p>`;
  installHelpTooltips(host);
}

function renderAnalysis(analysis) {
  const host = $("analysis-result");
  host.classList.remove("hidden");
  const ai = analysis.ai || {};
  const score = Number(analysis.score || 0);
  const verdicts = {
    attractive: "מעניין לבדיקה לקנייה", watch: "מתאים למעקב",
    cautious: "זהירות / המתנה", avoid_for_now: "לא אטרקטיבי כרגע",
  };
  const metrics = importantMetrics(analysis);
  const entry = renderEntryGuidance(analysis.entry_guidance || {});
  const categoryScores = renderCategoryScores(analysis.score_breakdown || {});
  host.innerHTML = `
    <div class="analysis-top">
      <div><p class="eyebrow">${escapeHtml(analysis.asset_type === "fund" ? "Fund analysis" : "Stock analysis")}</p><h3>${escapeHtml(analysis.name)} <bdi>(${escapeHtml(analysis.symbol)})</bdi></h3><p>${escapeHtml(ai.headline || "")}</p></div>
      <div class="score-ring" style="--score:${score * 3.6}deg"><strong>${score}</strong><span>/100</span></div>
    </div>
    <div class="verdict-line"><strong>${escapeHtml(verdicts[ai.verdict] || ai.verdict || "ממתין")}</strong><span>ביטחון ${Number(ai.confidence || 0)}% · איכות נתונים ${escapeHtml(analysis.data_quality || "—")}</span></div>
    ${entry}
    <section class="score-section"><div class="section-title"><h4>דירוג לפי קטגוריה</h4><span>כל הציונים מתוך 100</span></div>${categoryScores}</section>
    <section class="score-section"><div class="section-title"><h4>מדדים מרכזיים</h4><span>ערך ודירוג</span></div><div class="analysis-metrics">${metrics.map(([label, value, scoreKey]) => renderMetricCard(label, value, analysis.metric_scores?.[scoreKey])).join("")}</div></section>
    <p class="analysis-summary">${escapeHtml(ai.summary || "")}</p>
    <div class="pros-cons"><div><h4>נקודות חוזקה</h4>${listHtml(ai.positives, "positive")}</div><div><h4>סיכונים</h4>${listHtml(ai.risks, "negative")}</div></div>
    <div class="decision-box"><span>התאמה לפרופיל שלך</span><strong>${escapeHtml(ai.suitability || "")}</strong><p>${escapeHtml(ai.decision || "")}</p></div>
    <p class="verification-note">✓ הניתוח נבדק שוב מול הנתונים המקוריים · הערכה לימודית בלבד, לא ייעוץ השקעות.</p>`;
  installHelpTooltips(host);
}

function renderEntryGuidance(guidance) {
  const currency = guidance.currency || "";
  const price = (value) => value == null ? "אין נתון" : `${Number(value).toLocaleString("he-IL", { maximumFractionDigits: 2 })}${currency ? ` ${escapeHtml(currency)}` : ""}`;
  const low = guidance.entry_zone_low;
  const high = guidance.entry_zone_high;
  const zone = low == null || high == null ? "אין מספיק נתונים" : `${price(low)}–${price(high)}`;
  const comparison = guidance.current_vs_reference_pct == null ? "אין נתון" : `${Number(guidance.current_vs_reference_pct) >= 0 ? "+" : ""}${Number(guidance.current_vs_reference_pct).toFixed(1)}%`;
  const conditions = Array.isArray(guidance.conditions_he) ? guidance.conditions_he.slice(0, 3) : [];
  return `<section class="entry-guidance-card" data-help="מציג את המחיר הנוכחי מול מחיר ייחוס, טווח כניסה לימודי ותנאים שחשוב לבדוק לפני החלטה.">
    <div class="section-title"><div><p class="eyebrow">Entry plan</p><h4>מתי ובאיזה מחיר לשקול קנייה</h4></div><span class="entry-status">${escapeHtml(guidance.status_label_he || "אין מספיק נתונים")}</span></div>
    <div class="price-comparison-grid">
      <div><span>מחיר נוכחי</span><strong>${price(guidance.current_price)}</strong></div>
      <div><span>מחיר ייחוס</span><strong>${price(guidance.reference_price)}</strong><small>${escapeHtml(guidance.reference_label_he || "לא זמין")}</small></div>
      <div class="preferred-price"><span>טווח כניסה לימודי</span><strong>${zone}</strong><small>מרווח ביטחון ${guidance.margin_of_safety_pct == null ? "—" : `${Number(guidance.margin_of_safety_pct).toFixed(0)}%`}</small></div>
      <div><span>נוכחי מול ייחוס</span><strong class="${Number(guidance.current_vs_reference_pct) > 0 ? "negative" : Number(guidance.current_vs_reference_pct) < 0 ? "positive" : ""}">${comparison}</strong></div>
    </div>
    ${conditions.length ? `<ul class="entry-conditions">${conditions.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}
    <p class="method-note">${escapeHtml(guidance.methodology_he || "טווח לימודי בלבד; אינו הוראת קנייה.")}</p>
  </section>`;
}

function renderCategoryScores(scores) {
  const labels = {
    valuation: "תמחור ומכפילים", quality: "איכות ורווחיות", growth: "צמיחה",
    financial_health_and_risk: "בריאות פיננסית וסיכון", cost: "עלויות",
    performance: "ביצועים", diversification: "פיזור", risk: "סיכון",
  };
  const entries = Object.entries(scores);
  if (!entries.length) return `<p class="method-note">אין ציוני קטגוריות זמינים.</p>`;
  return `<div class="category-score-grid">${entries.map(([key, score]) => renderScoreTile(labels[key] || key, score)).join("")}</div>`;
}

function renderScoreTile(label, score) {
  const value = score == null ? null : Math.max(0, Math.min(100, Number(score)));
  const scoreClass = value == null ? "neutral" : value >= 65 ? "good" : value >= 45 ? "medium" : "weak";
  const help = `${label}: ציון יחסי מתוך 100 לפי הנתונים הזמינים; ציון גבוה מציין מצב עדיף במדד הזה.`;
  return `<div class="score-tile ${scoreClass}" data-help="${escapeHtml(help)}"><div><span>${escapeHtml(label)}</span><strong>${value == null ? "—" : Math.round(value)}<small>/100</small></strong></div><div class="score-bar"><i style="width:${value || 0}%"></i></div></div>`;
}

function renderMetricCard(label, value, score) {
  const help = `${label}: הערך שנמדד עבור הנכס ולצדו דירוג יחסי מתוך 100.`;
  return `<div class="metric-score-card" data-help="${escapeHtml(help)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong>${renderScoreTile("דירוג", score)}</div>`;
}

function importantMetrics(analysis) {
  const m = analysis.metrics || {};
  if (analysis.asset_type === "fund") return [
    ["דמי ניהול", formatPct(m.expense_ratio_pct), "expense_ratio"], ["תשואה שנתית", formatPct(m.return_1y_pct), "return_1y"],
    ["תשואה שנתית 3Y", formatPct(m.return_3y_annualized_pct), "return_3y"], ["תנודתיות", formatPct(m.volatility_1y_pct), "volatility"],
    ["10 החזקות מובילות", formatPct(m.top_10_weight_pct), "top_10_weight"], ["משיכה מרבית", formatPct(m.max_drawdown_5y_pct), "max_drawdown"],
  ];
  return [
    ["מכפיל רווח", formatNumber(m.forward_pe ?? m.trailing_pe), "pe_ratio"], ["תשואה על ההון", formatPct(m.return_on_equity_pct), "return_on_equity"],
    ["צמיחת הכנסות", formatPct(m.revenue_growth_pct), "revenue_growth"], ["שולי רווח", formatPct(m.profit_margin_pct), "profit_margin"],
    ["חוב להון", formatNumber(m.debt_to_equity), "debt_to_equity"], ["תשואה שנתית", formatPct(m.return_1y_pct), "return_1y"],
  ];
}

function listHtml(items, className) {
  const values = Array.isArray(items) ? items : [];
  return values.length ? `<ul class="${className}">${values.slice(0, 4).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : "<p>לא זמין</p>";
}

function populateTaxTickers(holdings) {
  const select = $("tax-ticker");
  const previous = select.value;
  select.innerHTML = "";
  for (const [ticker, holding] of Object.entries(holdings)) {
    const option = document.createElement("option");
    option.value = ticker;
    option.textContent = displayLabel(ticker, holding);
    select.append(option);
  }
  if (previous && holdings[previous]) select.value = previous;
  const selected = holdings[select.value];
  $("tax-price").value = selected?.current_price_account_currency > 0
    ? Number(selected.current_price_account_currency).toFixed(4).replace(/0+$/, "").replace(/\.$/, "")
    : "";
}

$("tax-ticker").addEventListener("change", () => {
  const holding = currentValuation?.holdings?.[$("tax-ticker").value];
  $("tax-price").value = holding?.current_price_account_currency > 0
    ? Number(holding.current_price_account_currency).toFixed(4).replace(/0+$/, "").replace(/\.$/, "")
    : "";
});

$("tax-current-btn").addEventListener("click", () => {
  const holding = currentValuation?.holdings?.[$("tax-ticker").value];
  const currentPrice = numberOrZero(holding?.current_price_account_currency);
  if (!(currentPrice > 0)) {
    $("tax-result").classList.remove("hidden");
    $("tax-result").textContent = "אין מחיר נוכחי זמין לנייר הזה.";
    return;
  }
  $("tax-price").value = currentPrice.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  $("tax-sim-btn").click();
});

function estimateWebSaleCommission(ticker, holding, quantity, proceeds) {
  const quoteCurrency = String(holding?.quote_currency || "").toUpperCase();
  const isForeign = ["USD", "EUR"].includes(quoteCurrency) || numberOrZero(holding?.buy_fx_rate) > 0;
  if (isForeign) {
    const fxRate = numberOrZero(holding?.current_fx_rate || holding?.fx_rate_to_account) || 1;
    if (quantity <= 1) {
      return { amount: 2.5 * fxRate, label: `מסחר בשבר עד יחידה אחת — $2.50 (${formatNumber(2.5 * fxRate)} ₪)` };
    }
    const amountUsd = Math.max(quantity * 0.01, 4.9);
    return { amount: amountUsd * fxRate, label: `1 סנט למניה, מינימום $4.90 (${amountUsd.toFixed(2)} USD)` };
  }
  const name = String(holding?.name || KNOWN_SECURITY_NAMES[ticker] || "");
  const isTrackingFund = name.includes("מחקה");
  const isEtf = name.includes("סל");
  const isOtherMutualFund = !isTrackingFund && !isEtf && (name.includes("קרן") || name.toLowerCase().includes("fund"));
  const minimum = isOtherMutualFund ? 16 : 1.9;
  const label = isEtf
    ? "0.059%, מינימום 1.90 ₪ לקרן סל בת״א במסלול רציף"
    : isTrackingFund
      ? "0.059%, מינימום 1.90 ₪ לקרן נאמנות מחקה בת״א"
      : isOtherMutualFund
        ? "0.059%, מינימום 16.00 ₪ לקרן נאמנות רגילה בת״א"
        : "0.059%, מינימום 1.90 ₪ לנייר בת״א שאינו קרן";
  return {
    amount: Math.max(proceeds * 0.00059, minimum),
    label,
  };
}

$("tax-sim-btn").addEventListener("click", () => {
  const ticker = $("tax-ticker").value;
  const holding = currentValuation?.holdings?.[$("tax-ticker").value];
  $("tax-result").classList.remove("hidden");
  if (!holding) {
    $("tax-result").textContent = "אין החזקה זמינה לחישוב.";
    return;
  }
  const quantity = $("tax-qty").value ? Number($("tax-qty").value) : Number(holding.quantity);
  const sellPrice = $("tax-price").value ? Number($("tax-price").value) : Number(holding.current_price_account_currency);
  if (!(quantity > 0) || quantity > holding.quantity || !(sellPrice > 0)) {
    $("tax-result").textContent = "הכמות או המחיר אינם תקינים.";
    return;
  }
  const proceeds = quantity * sellPrice;
  const exactHoldingCost = numberOrZero(holding.cost_basis);
  const cost = exactHoldingCost > 0 && numberOrZero(holding.quantity) > 0
    ? quantity / numberOrZero(holding.quantity) * exactHoldingCost
    : quantity * numberOrZero(holding.buy_price_account_currency ?? (
      numberOrZero(holding.buy_price) * numberOrZero(holding.unit_scale || 1)
    ));
  const commission = estimateWebSaleCommission(ticker, holding, quantity, proceeds);
  const grossGain = proceeds - cost;
  const taxableGain = proceeds - commission.amount - cost;
  const tax = Math.max(taxableGain, 0) * CAPITAL_GAINS_RATE;
  const netProceeds = proceeds - commission.amount - tax;
  const gainClass = grossGain >= 0 ? "tax-positive" : "tax-negative";
  const taxableClass = taxableGain >= 0 ? "tax-positive" : "tax-negative";
  $("tax-result").innerHTML = `
    <div class="tax-result-heading">
      <div><span>תרחיש מכירה</span><strong>${escapeHtml(displayLabel(ticker, holding))}</strong></div>
      <span class="tax-estimate-badge">הדמיה בלבד</span>
    </div>
    <div class="tax-result-grid">
      <div class="tax-result-item"><span>תמורה ברוטו</span><strong>${formatMoney(proceeds)}</strong></div>
      <div class="tax-result-item"><span>עלות מקורית</span><strong>${formatMoney(cost)}</strong></div>
      <div class="tax-result-item ${gainClass}"><span>רווח / הפסד לפני עמלה</span><strong>${formatSignedMoney(grossGain)}</strong></div>
      <div class="tax-result-item tax-fee"><span>עמלת מסחר צפויה</span><strong>${formatDeductionMoney(commission.amount)}</strong></div>
      <div class="tax-result-item ${taxableClass}"><span>רווח / הפסד לאחר עמלה</span><strong>${formatSignedMoney(taxableGain)}</strong></div>
      <div class="tax-result-item tax-tax"><span>מס משוער</span><strong>${formatDeductionMoney(tax)}</strong></div>
      <div class="tax-result-item tax-net"><span>נטו לאחר עמלה ומס</span><strong>${formatMoney(netProceeds)}</strong></div>
    </div>
    <div class="tax-commission-basis"><span>איך חושבה העמלה?</span><strong>${escapeHtml(commission.label)}</strong></div>
    <p class="tax-disclaimer">הסכומים בשקלים. חישוב לימודי לפי הנספח שסיפקת ומס רווחי הון משוער של 25%; שום דבר אינו נמכר בפועל.</p>`;
});

function renderTransactions(rows) {
  const host = $("tx-list");
  host.innerHTML = "";
  const filtered = rows.filter((tx) => tx.type && tx.type !== "system");
  if (!filtered.length) {
    host.append(emptyNote("אין עדיין פעילות רשומה."));
    return;
  }
  for (const tx of filtered) {
    const row = document.createElement("div");
    row.className = "tx-row";
    const labels = {
      buy: "קנייה", sell: "מכירה", import: "ייבוא", financial_asset_add: "הוספת חיסכון", financial_asset_update: "עדכון חיסכון",
      sync_added: "נוסף בסנכרון", sync_updated: "עודכן בסנכרון",
      sync_removed: "נמכר/הוסר בסנכרון", portfolio_sync: "סנכרון תיק", partial_portfolio_sync: "עדכון טבלת ברוקר",
    };
    const holding = currentValuation?.holdings?.[tx.ticker];
    const when = tx.timestamp?.toDate ? tx.timestamp.toDate().toLocaleDateString("he-IL") : "";
    const isFinancialAsset = tx.type === "financial_asset_add";
    const isSyncSummary = ["portfolio_sync", "partial_portfolio_sync"].includes(tx.type);
    const label = isSyncSummary ? "תמונת תיק מהקובץ"
      : isFinancialAsset ? `<bdi>${escapeHtml(tx.name || "מכשיר פיננסי")}</bdi>`
        : displayLabelHtml(tx.ticker, holding || { name: tx.name });
    const transactionPrice = AGOROT_SECURITY_CODES.has(String(tx.ticker || ""))
      ? numberOrZero(tx.price) / 100
      : numberOrZero(tx.price);
    const detail = isSyncSummary
      ? `נוספו ${numberOrZero(tx.added)}, עודכנו ${numberOrZero(tx.updated)}, הוסרו ${numberOrZero(tx.removed)}, ללא שינוי ${numberOrZero(tx.unchanged)}`
      : tx.type === "sync_updated"
        ? `כמות ${formatQuantity(tx.old_quantity)} ← ${formatQuantity(tx.new_quantity)} · מחיר בסיס ${formatMoney(transactionPrice)}`
        : isFinancialAsset ? `צבירה ${formatMoney(tx.amount)}` : `${formatQuantity(tx.quantity)} יח' במחיר ${formatMoney(transactionPrice)}`;
    row.innerHTML = `<span class="tx-icon ${tx.type === "sell" ? "sell" : "buy"}">${tx.type === "sell" ? "−" : "+"}</span><span><strong>${escapeHtml(labels[tx.type] || tx.type)} · ${label}</strong><small>${detail}</small></span><time>${escapeHtml(when)}</time>`;
    host.append(row);
  }
}

function toggleRequestButtons(disabled) {
  $("analysis-btn").disabled = disabled;
  $("thinking-btn").disabled = disabled;
  $("ai-recommend-btn").disabled = disabled;
  $("ai-ask-btn").disabled = disabled;
}

function setSyncState(state, text) {
  $("sync-status").className = `sync-pill ${state}`;
  $("sync-status").innerHTML = `<span></span> ${escapeHtml(text)}`;
}

function displayLabel(ticker, details) {
  return details?.name || KNOWN_SECURITY_NAMES[ticker] || ticker;
}

function displayLabelHtml(ticker, details) {
  const name = details?.name || KNOWN_SECURITY_NAMES[ticker];
  return name ? `<bdi>${escapeHtml(name)}</bdi>` : `<bdi>${escapeHtml(ticker)}</bdi>`;
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

function numberOrZero(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

function helpDescription(element) {
  const explicit = String(element.dataset.help || "").trim();
  if (explicit) return explicit;
  const text = (element.textContent || "").replace(/\s+/g, " ").trim();
  const explanations = [
    ["ניתוח פונדמנטלי", "בדיקה של תמחור, רווחיות, צמיחה, חוב וסיכון כדי להעריך את איכות הנכס."],
    ["חשבון מסחר", "סיכום המזומן, שווי הנכסים והרווח או ההפסד בחשבון המסחר בלבד."],
    ["קופות וחסכונות", "סיכום קופות הגמל ומכשירי החיסכון שמנוהלים לצד תיק המסחר."],
    ["הרכב התיק", "מציג כמה מהכסף נמצא בכל נכס, מטבע, סקטור או שוק."],
    ["החזקות", "הנכסים שבתיק, השווי שלהם והתשואה האישית מאז מחיר הבסיס."],
    ["יעד", "השווי הנוכחי ביחס לסכום שאליו ברצונך להגיע."],
    ["קופות גמל", "חיסכון ארוך טווח; היתרה המוצגת היא אומדן בין דיווחים חודשיים."],
    ["קנייה וייבוא", "סנכרון מאובטח של תמונת תיק המסחר מהברוקר."],
    ["מזומן", "כסף פנוי שאינו מושקע כרגע ונכלל בשווי חשבון המסחר."],
    ["פרופיל", "העדפות הסיכון, הטווח והמטרה שמשמשות להתאמת תשובות ה-AI."],
    ["שאל את ה־AI", "שאלות וניתוח מלא של התיק; שינוי כספי דורש אישור מפורש."],
    ["סימולטור מס", "אומדן לימודי לתרחיש מכירה; אינו חישוב מס רשמי."],
    ["פעילות", "רשימת קניות, מכירות ושינויי סנכרון אחרונים."],
    ["רווח / הפסד", "ההפרש בין השווי הנוכחי לעלות הקנייה, מאז מועד הקנייה או מחיר הבסיס שהוזן."],
    ["רווח/הפסד", "ההפרש בין השווי הנוכחי לעלות הקנייה, מאז מועד הקנייה או מחיר הבסיס שהוזן."],
    ["דירוג לפי קטגוריה", "ציון יחסי מתוך 100 לכל היבט בניתוח; הוא כלי השוואה לימודי ולא הבטחת תשואה."],
    ["תמחור ומכפילים", "בודק אם מחיר הנכס נראה גבוה או נמוך ביחס לרווחים ולנתוני השוואה."],
    ["איכות ורווחיות", "מעריך את יציבות העסק, שולי הרווח והתשואה שהוא מפיק מההון."],
    ["בריאות פיננסית וסיכון", "בודק חוב, נזילות, יציבות פיננסית ותנודתיות אפשרית."],
    ["צמיחה", "קצב השינוי בהכנסות וברווחים; צמיחה בעבר אינה מבטיחה צמיחה בעתיד."],
    ["ביצועים", "השינוי במחיר בתקופה המוצגת, לפני התאמה למחיר הקנייה האישי שלך."],
    ["פיזור", "מידת הפיזור בין נכסים; פיזור רחב יכול לצמצם תלות בנכס יחיד."],
    ["סיכון", "הערכת אי-הוודאות והאפשרות לירידות; ציון גבוה מציין מצב עדיף לפי המדדים שנבדקו."],
    ["דמי ניהול", "העלות השנתית שגובה הקרן או הקופה, ומופחתת לאורך זמן מהתשואה."],
    ["תנודתיות", "עוצמת העליות והירידות במחיר; תנודתיות גבוהה משמעה טווח תוצאות רחב יותר."],
    ["משיכה מרבית", "הירידה הגדולה ביותר משיא לשפל בתקופה שנבדקה."],
    ["תשואה על ההון", "כמה רווח החברה מייצרת ביחס להון בעלי המניות."],
    ["חוב להון", "יחס החוב להון העצמי; יחס גבוה עשוי להעיד על מינוף וסיכון גדולים יותר."],
    ["שולי רווח", "אחוז ההכנסה שנשאר כרווח לאחר הוצאות החברה."],
    ["מחיר נוכחי", "המחיר האחרון שנשלף, במטבע שמוצג בכרטיס ובמועד העדכון המצוין."],
    ["טווח כניסה", "טווח מחיר לימודי לשקילת קנייה לפי הנתונים הזמינים ומרווח הביטחון."],
    ["מחיר ייחוס", "מחיר השוואה לימודי המבוסס על נתוני התמחור הזמינים, לא יעד מובטח."],
    ["מכפיל רווח", "מחיר המניה ביחס לרווח השנתי שלה; נמוך אינו תמיד זול ואיכות העסק חשובה."],
    ["מטבע", "חלוקת שווי התיק לפי מטבע החשיפה או המסחר של כל נכס."],
    ["סקטור", "חלוקת ההשקעות לפי תחומי פעילות כמו טכנולוגיה, פיננסים או תעשייה."],
    ["מדינות ושווקים", "חלוקת החשיפה לפי מדינה או שוק שאליהם הנכסים קשורים."],
    ["שווקים ומדינות", "חלוקת החשיפה לפי מדינה או שוק שאליהם הנכסים קשורים."],
    ["דרוג AI", "הערכה יחסית של ה-AI מתוך 100 על סמך הנתונים הזמינים; אינה המלצת השקעה מחייבת."],
    ["דירוג AI", "הערכה יחסית של ה-AI מתוך 100 על סמך הנתונים הזמינים; אינה המלצת השקעה מחייבת."],
    ["תשואה", "השינוי בשווי מאז נקודת הבסיס, בשקלים ובאחוזים."],
    ["שינוי יומי", "השינוי ביום המסחר האחרון בלבד, ולא הרווח האישי מאז הקנייה."],
    ["עלות", "הסכום ששימש כבסיס לחישוב הרווח וההפסד."],
    ["שווי", "הערך המשוער לפי המחיר ושער המטבע האחרונים שנשלפו."],
  ];
  const match = explanations.find(([keyword]) => text.includes(keyword));
  if (match) return match[1];
  return null;
}

function installHelpTooltips(root = document) {
  root.querySelectorAll(".panel, .metric-card, .score-tile, .metric-score-card, .entry-guidance-card").forEach((element) => {
    if (element.querySelector(":scope > .info-tip")) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "info-tip";
    button.textContent = "?";
    const description = helpDescription(element);
    if (!description) return;
    button.dataset.tooltip = description;
    button.title = description;
    button.setAttribute("aria-label", description);
    element.prepend(button);
  });
}

installHelpTooltips();

function formatMoney(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  try {
    return new Intl.NumberFormat("he-IL", { style: "currency", currency: currentProfile.base_currency || "ILS", maximumFractionDigits: 2 }).format(number);
  } catch {
    return number.toFixed(2);
  }
}

function formatQuoteMoney(value, currency) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 4,
    }).format(number);
  } catch {
    return `${number.toFixed(4)} ${currency}`;
  }
}

function formatSignedMoneyAndPercent(value, percent) {
  const amount = numberOrZero(value);
  const percentage = numberOrZero(percent);
  const sign = amount > 0 ? "+" : amount < 0 ? "−" : "";
  const formattedAmount = new Intl.NumberFormat("he-IL", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
    useGrouping: true,
  }).format(Math.abs(amount));
  return `${sign}₪${formattedAmount} · ${percentage.toFixed(1)}%`;
}

function formatSignedMoney(value) {
  const amount = numberOrZero(value);
  const sign = amount > 0 ? "+" : amount < 0 ? "−" : "";
  return `${sign}${formatMoney(Math.abs(amount))}`;
}

function formatDeductionMoney(value) {
  const amount = Math.max(numberOrZero(value), 0);
  return amount > 0 ? `−${formatMoney(amount)}` : formatMoney(0);
}

function formatQuantity(value) {
  return new Intl.NumberFormat("he-IL", { maximumFractionDigits: 4 }).format(numberOrZero(value));
}

function formatPct(value) {
  return value == null || !Number.isFinite(Number(value)) ? "—" : `${Number(value).toFixed(1)}%`;
}

function formatNumber(value) {
  return value == null || !Number.isFinite(Number(value)) ? "—" : Number(value).toFixed(2);
}

function formatIsoTime(value) {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleString("he-IL", { dateStyle: "short", timeStyle: "short" });
}

function trendClass(value) {
  return value == null ? "" : Number(value) > 0 ? "gain" : Number(value) < 0 ? "loss" : "";
}

function setTrendClass(element, value) {
  element.classList.toggle("gain", Number(value) > 0);
  element.classList.toggle("loss", Number(value) < 0);
}

function showMessage(id, text, success) {
  const element = $(id);
  element.textContent = text;
  element.className = `form-message ${success ? "success" : "error-text"}`;
}

function emptyNote(text) {
  const node = document.createElement("div");
  node.className = "empty-state";
  node.textContent = text;
  return node;
}
