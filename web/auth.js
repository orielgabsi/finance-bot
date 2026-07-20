import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import {
  getAuth,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  createUserWithEmailAndPassword,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";
import { firebaseConfig } from "./firebase-config.js";

const app = initializeApp(firebaseConfig);
const auth = getAuth(app);

let mode = "signin"; // or "signup"

const emailInput = document.getElementById("email");
const passwordInput = document.getElementById("password");
const submitBtn = document.getElementById("submit-btn");
const errorMsg = document.getElementById("error-msg");
const formTitle = document.getElementById("form-title");
const toggleText = document.getElementById("toggle-text");
const toggleLink = document.getElementById("toggle-link");

function setMode(newMode) {
  mode = newMode;
  if (mode === "signin") {
    formTitle.textContent = "התחברות";
    submitBtn.textContent = "התחבר";
    toggleText.textContent = "אין לך חשבון? ";
    toggleLink.textContent = "הרשמה";
  } else {
    formTitle.textContent = "הרשמה";
    submitBtn.textContent = "הרשם";
    toggleText.textContent = "כבר יש לך חשבון? ";
    toggleLink.textContent = "התחברות";
  }
  errorMsg.textContent = "";
}

toggleLink.addEventListener("click", () => setMode(mode === "signin" ? "signup" : "signin"));

// Already signed in? Skip straight to the dashboard.
onAuthStateChanged(auth, (user) => {
  if (user) window.location.href = "dashboard.html";
});

submitBtn.addEventListener("click", async () => {
  const email = emailInput.value.trim();
  const password = passwordInput.value;
  errorMsg.textContent = "";

  if (!email || !password) {
    errorMsg.textContent = "נא למלא אימייל וסיסמה.";
    return;
  }

  try {
    if (mode === "signin") {
      await signInWithEmailAndPassword(auth, email, password);
    } else {
      await createUserWithEmailAndPassword(auth, email, password);
    }
    window.location.href = "dashboard.html";
  } catch (err) {
    errorMsg.textContent = friendlyError(err.code);
  }
});

function friendlyError(code) {
  switch (code) {
    case "auth/invalid-email": return "כתובת אימייל לא תקינה.";
    case "auth/user-not-found":
    case "auth/wrong-password":
    case "auth/invalid-credential": return "אימייל או סיסמה שגויים.";
    case "auth/email-already-in-use": return "כבר קיים חשבון עם אימייל זה.";
    case "auth/weak-password": return "הסיסמה חייבת להכיל לפחות 6 תווים.";
    default: return "שגיאה: " + code;
  }
}
