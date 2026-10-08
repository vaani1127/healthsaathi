import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import en from "./locales/en.json";
import hi from "./locales/hi.json";

export const SUPPORTED_LANGUAGES = ["en", "hi"] as const;
export type Language = (typeof SUPPORTED_LANGUAGES)[number];

const STORAGE_KEY = "hs.lang";

function readStoredLanguage(): Language {
  try {
    return localStorage.getItem(STORAGE_KEY) === "hi" ? "hi" : "en";
  } catch {
    return "en";
  }
}

void i18n.use(initReactI18next).init({
  resources: { en: { translation: en }, hi: { translation: hi } },
  lng: readStoredLanguage(),
  fallbackLng: "en",
  interpolation: { escapeValue: false },
});

i18n.on("languageChanged", (lng) => {
  document.documentElement.lang = lng;
  try {
    localStorage.setItem(STORAGE_KEY, lng);
  } catch {
    // Storage can be blocked in private browsing. The choice then lasts for this tab only.
  }
});

export default i18n;
