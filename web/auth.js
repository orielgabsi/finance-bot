import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import {
  getAuth,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  createUserWithEmailAndPassword,
  sendPasswordResetEmail,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";
import { firebaseConfig } from "./firebase-config.js";

const app = initializeApp(firebaseConfig);
const auth = getAuth(app);

let mode = "signin"; // "signin" | "signup" | "reset"

const emailInput = document.getElementById("email");
const passwordInput = document.getElementById("password");
const passwordHint = document.getElementById("password-hint");
const confirmField = document.getElementById("confirm-password-field");
const confirmInput = document.getElementById("confirm-password");
const submitBtn = document.getElementById("submit-btn");
const errorMsg = document.getElementById("error-msg");
const successMsg = document.getElementById("success-msg");
const formTitle = document.getElementById("form-title");
const toggleText = document.getElementById("toggle-text");
const toggleLink = document.getElementById("toggle-link");
const forgotLink = document.getElementById("forgot-link");

function setMode(newMode) {
  mode = newMode;
  errorMsg.textContent = "";
  successMsg.textContent = "";
  successMsg.classList.add("hidden");

  const passwordVisible = mode !== "reset";
  passwordInput.classList.toggle("hidden", !passwordVisible);
  document.querySelector('label[for="password"]').classList.toggle("hidden", !passwordVisible);
  passwordHint.classList.toggle("hidden", mode !== "signup");
  confirmField.classList.toggle("hidden", mode !== "signup");
  forgotLink.parentElement.classList.toggle("hidden", mode === "reset");

  if (mode === "signin") {
    formTitle.textContent = "התחברות";
    submitBtn.textContent = "התחבר";
    toggleText.textContent = "אין לך חשבון? ";
    toggleLink.textContent = "הרשמה";
    passwordInput.autocomplete = "current-password";
  } else if (mode === "signup") {
    formTitle.textContent = "הרשמה";
    submitBtn.textContent = "צור חשבון";
    toggleText.textContent = "כבר יש לך חשבון? ";
    toggleLink.textContent = "התחברות";
    passwordInput.autocomplete = "new-password";
  } else {
    formTitle.textContent = "איפוס סיסמה";
    submitBtn.textContent = "שלח קישור לאיפוס";
    toggleText.textContent = "";
    toggleLink.textContent = "חזרה להתחברות";
  }
}

toggleLink.addEventListener("click", () => setMode(mode === "signin" ? "signup" : "signin"));
forgotLink.addEventListener("click", () => setMode("reset"));

// Already signed in? Skip straight to the dashboard.
onAuthStateChanged(auth, (user) => {
  if (user) window.location.href = "dashboard.html";
});

submitBtn.addEventListener("click", async () => {
  const email = emailInput.value.trim();
  const password = passwordInput.value;
  errorMsg.textContent = "";
  successMsg.classList.add("hidden");

  if (!email) {
    errorMsg.textContent = "נא למלא כתובת אימייל.";
    return;
  }

  if (mode === "reset") {
    try {
      await sendPasswordResetEmail(auth, email);
      successMsg.textContent = "אם קיים חשבון עם אימייל זה, נשלח אליו קישור לאיפוס הסיסמה. בדוק גם בתיקיית הספאם.";
      successMsg.classList.remove("hidden");
    } catch (err) {
      // Firebase can also throw auth/user-not-found here; show the same
      // generic message either way so the reset flow can't be used to probe
      // which emails have an account.
      if (err.code === "auth/invalid-email") {
        errorMsg.textContent = friendlyError(err.code);
      } else {
        successMsg.textContent = "אם קיים חשבון עם אימייל זה, נשלח אליו קישור לאיפוס הסיסמה. בדוק גם בתיקיית הספאם.";
        successMsg.classList.remove("hidden");
      }
    }
    return;
  }

  if (!password) {
    errorMsg.textContent = "נא למלא סיסמה.";
    return;
  }

  if (mode === "signup") {
    if (password.length < 6) {
      errorMsg.textContent = "הסיסמה חייבת להכיל לפחות 6 תווים.";
      return;
    }
    if (password !== confirmInput.value) {
      errorMsg.textContent = "אימות הסיסמה אינו תואם לסיסמה שהוזנה.";
      return;
    }
  }

  submitBtn.disabled = true;
  try {
    if (mode === "signin") {
      await signInWithEmailAndPassword(auth, email, password);
    } else {
      await createUserWithEmailAndPassword(auth, email, password);
    }
    window.location.href = "dashboard.html";
  } catch (err) {
    errorMsg.textContent = friendlyError(err.code);
  } finally {
    submitBtn.disabled = false;
  }
});

function friendlyError(code) {
  switch (code) {
    case "auth/invalid-email": return "כתובת אימייל לא תקינה.";
    case "auth/user-not-found":
    case "auth/wrong-password":
    case "auth/invalid-credential": return "אימייל או סיסמה שגויים.";
    case "auth/email-already-in-use": return "כבר קיים חשבון עם אימייל זה. נסה להתחבר או לאפס סיסמה.";
    case "auth/weak-password": return "הסיסמה חייבת להכיל לפחות 6 תווים.";
    case "auth/too-many-requests": return "יותר מדי ניסיונות. נסה שוב בעוד כמה דקות.";
    case "auth/network-request-failed": return "בעיית רשת. בדוק את החיבור לאינטרנט ונסה שוב.";
    default: return "שגיאה: " + code;
  }
}
