import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import { getAuth, onAuthStateChanged, signOut } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";
import {
  getFirestore,
  doc,
  getDoc,
  setDoc,
  addDoc,
  onSnapshot,
  collection,
  query,
  orderBy,
  limit,
  getDocs,
  serverTimestamp,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { firebaseConfig } from "./firebase-config.js";

// Rough capital-gains estimate only — NOT tax advice. Mirrors tax_service.py's
// flat 25% Israeli rate; no CPI adjustment, no loss offsetting. Kept in sync
// with that file manually since the website has no build step to share code.
const CAPITAL_GAINS_RATE = 0.25;

const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
const db = getFirestore(app);

const linkSection = document.getElementById("link-section");
const dashboardSection = document.getElementById("dashboard-section");
const linkCodeInput = document.getElementById("link-code");
const linkBtn = document.getElementById("link-btn");
const linkError = document.getElementById("link-error");
const logoutBtn = document.getElementById("logout-btn");

const taxTickerSelect = document.getElementById("tax-ticker");
const taxQtyInput = document.getElementById("tax-qty");
const taxPriceInput = document.getElementById("tax-price");
const taxSimBtn = document.getElementById("tax-sim-btn");
const taxResultEl = document.getElementById("tax-result");

const aiRecommendBtn = document.getElementById("ai-recommend-btn");
const aiQuestionInput = document.getElementById("ai-question");
const aiAskBtn = document.getElementById("ai-ask-btn");
const aiAnswerEl = document.getElementById("ai-answer");

const txListEl = document.getElementById("tx-list");

let chartInstance = null;
let currentTelegramId = null;
let currentValuation = null;
let aiUnsubscribe = null;

onAuthStateChanged(auth, async (user) => {
  if (!user) {
    window.location.href = "index.html";
    return;
  }
  await showLinkedStateOrPrompt(user.uid);
});

logoutBtn.addEventListener("click", () => signOut(auth));

linkBtn.addEventListener("click", async () => {
  const user = auth.currentUser;
  if (!user) return;
  const code = linkCodeInput.value.trim();
  linkError.textContent = "";

  if (!code) {
    linkError.textContent = "נא להזין קוד.";
    return;
  }

  try {
    const codeDoc = await getDoc(doc(db, "link_codes", code));
    if (!codeDoc.exists()) {
      linkError.textContent = "קוד לא תקין או שפג תוקפו. שלח /link שוב בבוט.";
      return;
    }
    const { telegram_id } = codeDoc.data();

    await setDoc(doc(db, "account_links", user.uid), {
      telegram_id,
      used_code: code,
      linked_at: serverTimestamp(),
    });

    await showLinkedStateOrPrompt(user.uid);
  } catch (err) {
    // A rules rejection (expired code, mismatch) surfaces here as permission-denied.
    linkError.textContent = "החיבור נכשל — הקוד שגוי או שפג תוקפו. שלח /link שוב בבוט.";
    console.error(err);
  }
});

async function showLinkedStateOrPrompt(uid) {
  const linkDoc = await getDoc(doc(db, "account_links", uid));

  if (!linkDoc.exists()) {
    linkSection.classList.remove("hidden");
    dashboardSection.classList.add("hidden");
    return;
  }

  linkSection.classList.add("hidden");
  dashboardSection.classList.remove("hidden");

  currentTelegramId = linkDoc.data().telegram_id;
  await loadPortfolio(currentTelegramId);
  await loadTransactions(currentTelegramId);
}

async function loadPortfolio(telegramId) {
  const userDoc = await getDoc(doc(db, "users", telegramId));
  const data = userDoc.exists() ? userDoc.data() : {};
  const valuation = data.last_valuation;
  const updatedAt = data.last_valuation_at;

  const updatedAtEl = document.getElementById("updated-at");
  const emptyMsg = document.getElementById("empty-msg");
  const canvas = document.getElementById("pie-chart");
  const holdingsListEl = document.getElementById("holdings-list");

  currentValuation = valuation && valuation.holdings ? valuation : null;
  populateTaxTickerOptions();

  if (!valuation || !valuation.holdings || Object.keys(valuation.holdings).length === 0) {
    setStat("stat-value", "—");
    setStat("stat-cost", "—");
    setStat("stat-gain", "—");
    setStat("stat-day-change", "—");
    emptyMsg.classList.remove("hidden");
    canvas.classList.add("hidden");
    updatedAtEl.textContent = "";
    holdingsListEl.innerHTML = "";
    return;
  }

  emptyMsg.classList.add("hidden");
  canvas.classList.remove("hidden");

  setStat("stat-value", formatMoney(valuation.total_value));
  setStat("stat-cost", formatMoney(valuation.total_cost));

  const gainEl = document.getElementById("stat-gain");
  gainEl.textContent = formatMoney(valuation.total_gain_loss) +
    ` (${valuation.total_gain_loss_pct.toFixed(1)}%)`;
  gainEl.classList.toggle("gain", valuation.total_gain_loss >= 0);
  gainEl.classList.toggle("loss", valuation.total_gain_loss < 0);

  const dayChangeEl = document.getElementById("stat-day-change");
  const dayChange = valuation.total_day_change_value;
  if (typeof dayChange === "number") {
    dayChangeEl.textContent = (dayChange >= 0 ? "+" : "") + formatMoney(dayChange);
    dayChangeEl.classList.toggle("gain", dayChange >= 0);
    dayChangeEl.classList.toggle("loss", dayChange < 0);
  } else {
    dayChangeEl.textContent = "—";
  }

  if (updatedAt && updatedAt.toDate) {
    updatedAtEl.textContent = "עודכן לאחרונה: " + updatedAt.toDate().toLocaleString("he-IL");
  }

  renderChart(valuation.holdings);
  renderHoldingsList(valuation.holdings);
}

function renderChart(holdings) {
  const labels = [];
  const values = [];
  for (const [ticker, h] of Object.entries(holdings)) {
    if (h.market_value) {
      // Unlike the bot's server-side matplotlib chart (which sticks to
      // tickers because matplotlib doesn't shape Hebrew text correctly),
      // Chart.js in the browser renders Hebrew fine, so show the name here.
      labels.push(displayLabel(ticker, h));
      values.push(h.market_value);
    }
  }

  if (chartInstance) chartInstance.destroy();
  chartInstance = new Chart(document.getElementById("pie-chart"), {
    type: "pie",
    data: {
      labels,
      datasets: [{
        data: values,
        backgroundColor: ["#4f46e5", "#06b6d4", "#f59e0b", "#ef4444", "#10b981", "#8b5cf6", "#ec4899"],
      }],
    },
    options: {
      plugins: { legend: { labels: { color: "#e2e8f0" } } },
    },
  });
}

// Many Israeli brokerage imports use an opaque numeric security number as the
// ticker; when a readable name was captured (portfolio_import.py's
// NAME_HEADERS), show it alongside the number instead of just the number.
// Plain-text version — use with .textContent (e.g. <option> labels), which
// doesn't parse HTML, so no <bdi> markup here.
function displayLabel(ticker, details) {
  const name = details && details.name;
  return name ? `${name} (${ticker})` : ticker;
}

// The "name" (and in principle the ticker) can originate from an imported
// spreadsheet or AI-parsed data, not just this app's own code — escape before
// interpolating into innerHTML so a crafted cell value can't inject markup.
function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

// HTML version for innerHTML contexts: a Hebrew page (dir="rtl") with an
// embedded English security name or numeric ticker in parentheses is exactly
// the case the Unicode bidi algorithm handles badly — punctuation can
// visually flip order depending on which script the run started with.
// <bdi> ("bidirectional isolation") is HTML's built-in fix: it renders its
// content as a self-contained unit without disturbing the surrounding
// Hebrew sentence's own reading order (the Python bot uses the equivalent
// Unicode LRI/PDI isolate characters for the same reason).
function displayLabelHtml(ticker, details) {
  const name = details && details.name;
  const tickerHtml = `<bdi>${escapeHtml(ticker)}</bdi>`;
  return name ? `<bdi>${escapeHtml(name)}</bdi> (${tickerHtml})` : tickerHtml;
}

function renderHoldingsList(holdings) {
  const el = document.getElementById("holdings-list");
  el.innerHTML = "";
  for (const [ticker, h] of Object.entries(holdings)) {
    const row = document.createElement("div");
    row.className = "holding-row";

    const gain = h.gain_loss;
    const arrow = gain == null ? "⚪" : gain > 0 ? "🟢▲" : gain < 0 ? "🔴▼" : "⚪";
    const gainText = gain == null ? "" : ` ${arrow} ${gain >= 0 ? "+" : ""}${gain.toFixed(2)}`;

    const dayPct = h.day_change_pct;
    const dayText = typeof dayPct === "number"
      ? ` · ${dayPct > 0 ? "📈" : dayPct < 0 ? "📉" : "➖"}${dayPct >= 0 ? "+" : ""}${dayPct.toFixed(1)}% היום`
      : "";

    // period_label says what period_change_pct actually measures ("השבוע"
    // via yfinance, "מתחילת החודש" via the Globes fallback) — the two price
    // sources don't expose the same timeframe.
    const periodPct = h.period_change_pct;
    const periodText = typeof periodPct === "number"
      ? ` · ${periodPct > 0 ? "📈" : periodPct < 0 ? "📉" : "➖"}${periodPct >= 0 ? "+" : ""}${periodPct.toFixed(1)}% ${h.period_label || "בתקופה"}`
      : "";

    // Quantity/buy-price deliberately omitted here — this list answers "how's
    // each asset doing", not a restatement of the raw holding record.
    row.innerHTML = `
      <span class="ticker">${displayLabelHtml(ticker, h)}</span>
      <span class="details">${gainText}${dayText}${periodText}</span>
    `;
    el.appendChild(row);
  }
}

function populateTaxTickerOptions() {
  taxTickerSelect.innerHTML = "";
  if (!currentValuation) {
    const opt = document.createElement("option");
    opt.textContent = "אין החזקות בתיק";
    taxTickerSelect.appendChild(opt);
    return;
  }
  for (const [ticker, h] of Object.entries(currentValuation.holdings)) {
    const opt = document.createElement("option");
    opt.value = ticker;
    opt.textContent = displayLabel(ticker, h); // textContent — safe against injection by construction
    taxTickerSelect.appendChild(opt);
  }
}

taxSimBtn.addEventListener("click", () => {
  taxResultEl.classList.remove("hidden");
  if (!currentValuation) {
    taxResultEl.textContent = "אין נתוני תיק זמינים.";
    return;
  }
  const ticker = taxTickerSelect.value;
  const holding = currentValuation.holdings[ticker];
  if (!holding) {
    taxResultEl.textContent = "בחר טיקר תקין.";
    return;
  }

  const heldQty = holding.quantity;
  const qtyRaw = taxQtyInput.value.trim();
  const qty = qtyRaw ? parseFloat(qtyRaw) : heldQty;
  if (!qty || qty <= 0 || qty > heldQty) {
    taxResultEl.textContent = `כמות לא תקינה — יש לך ${heldQty} ${ticker}.`;
    return;
  }

  const priceRaw = taxPriceInput.value.trim();
  const sellPrice = priceRaw ? parseFloat(priceRaw) : holding.current_price;
  if (!sellPrice || sellPrice <= 0) {
    taxResultEl.textContent = "לא זמין מחיר נוכחי — הזן מחיר מכירה ידנית.";
    return;
  }

  const proceeds = qty * sellPrice;
  const cost = qty * holding.buy_price;
  const gain = proceeds - cost;
  const estimatedTax = Math.max(gain, 0) * CAPITAL_GAINS_RATE;
  const netAfterTax = proceeds - estimatedTax;

  taxResultEl.textContent =
    `${qty} יח' × ${sellPrice.toFixed(2)} = תמורה ${formatMoney(proceeds)}\n` +
    `עלות מקורית: ${formatMoney(cost)}\n` +
    `רווח/הפסד: ${gain >= 0 ? "+" : ""}${formatMoney(gain)}\n` +
    `💸 מס משוער (25% שטוח, הערכה בלבד): ${formatMoney(estimatedTax)}\n` +
    `✅ נטו משוער אחרי מס: ${formatMoney(netAfterTax)}`;
});

// --- AI recommendation / Q&A ---
// The website has no backend of its own, so a question is written to
// Firestore and relayed to the already-running Telegram bot, which computes
// the answer (Groq + Tavily, same as the bot's own AI features) and writes
// it back. This avoids ever exposing the Groq/Tavily API keys to the browser.
async function submitAiQuestion(questionText) {
  if (!currentTelegramId) return;
  if (aiUnsubscribe) {
    aiUnsubscribe();
    aiUnsubscribe = null;
  }

  aiAnswerEl.classList.remove("hidden");
  aiAnswerEl.textContent = "🤖 חושב... (הבקשה מועברת לבוט, זה עשוי לקחת עד כמה שניות)";
  aiRecommendBtn.disabled = true;
  aiAskBtn.disabled = true;

  try {
    const reqRef = await addDoc(collection(db, "users", currentTelegramId, "ai_requests"), {
      question: questionText,
      status: "pending",
      created_at: serverTimestamp(),
    });

    aiUnsubscribe = onSnapshot(reqRef, (snap) => {
      const data = snap.data();
      if (!data || data.status !== "answered") return;
      aiAnswerEl.textContent = data.answer || "לא התקבלה תשובה.";
      aiRecommendBtn.disabled = false;
      aiAskBtn.disabled = false;
      if (aiUnsubscribe) {
        aiUnsubscribe();
        aiUnsubscribe = null;
      }
    });
  } catch (err) {
    aiAnswerEl.textContent = "שגיאה בשליחת הבקשה. נסה שוב.";
    aiRecommendBtn.disabled = false;
    aiAskBtn.disabled = false;
    console.error(err);
  }
}

aiRecommendBtn.addEventListener("click", () => {
  submitAiQuestion("תן לי המלצת AI קצרה על מצב התיק שלי כרגע.");
});

aiAskBtn.addEventListener("click", () => {
  const text = aiQuestionInput.value.trim();
  if (!text) return;
  submitAiQuestion(text);
  aiQuestionInput.value = "";
});

// --- Transaction history ---
async function loadTransactions(telegramId) {
  txListEl.innerHTML = "";
  try {
    const q = query(
      collection(db, "users", telegramId, "transactions"),
      orderBy("timestamp", "desc"),
      limit(20)
    );
    const snap = await getDocs(q);
    const rows = snap.docs
      .map((d) => d.data())
      .filter((tx) => tx.type && tx.type !== "system");

    if (rows.length === 0) {
      txListEl.innerHTML = '<div class="empty-note">אין עדיין עסקאות רשומות.</div>';
      return;
    }

    for (const tx of rows) {
      const row = document.createElement("div");
      row.className = "tx-row";
      const typeLabel = tx.type === "buy" ? "קנייה" : tx.type === "sell" ? "מכירה" : tx.type === "import" ? "ייבוא" : tx.type;
      const typeClass = tx.type === "sell" ? "tx-type-sell" : "tx-type-buy";
      const when = tx.timestamp && tx.timestamp.toDate ? tx.timestamp.toDate().toLocaleDateString("he-IL") : "";
      const tickerLabelHtml = currentValuation && currentValuation.holdings[tx.ticker]
        ? displayLabelHtml(tx.ticker, currentValuation.holdings[tx.ticker])
        : `<bdi>${escapeHtml(tx.ticker)}</bdi>`;
      row.innerHTML = `
        <span><span class="${typeClass}">${escapeHtml(typeLabel)}</span> ${tickerLabelHtml} · ${tx.quantity} יח' @ ${tx.price}</span>
        <span class="details">${escapeHtml(when)}</span>
      `;
      txListEl.appendChild(row);
    }
  } catch (err) {
    txListEl.innerHTML = '<div class="empty-note">לא ניתן היה לטעון היסטוריית עסקאות.</div>';
    console.error(err);
  }
}

function setStat(id, text) {
  document.getElementById(id).textContent = text;
}

function formatMoney(n) {
  return typeof n === "number" ? n.toFixed(2) : "—";
}
