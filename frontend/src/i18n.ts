import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import en from "./locales/en.json";
import zh from "./locales/zh.json";

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: { "en-US": { translation: en }, "zh-CN": { translation: zh } },
    fallbackLng: "zh-CN",
    interpolation: { escapeValue: false },
  });

export default i18n;
