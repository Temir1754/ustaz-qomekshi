const BACKEND_URL = "http://194.32.141.231:8092";

const statusEl = document.getElementById("status");
const codeEl = document.getElementById("code");
const btn = document.getElementById("connect");

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = cls || "";
}

// Ищет id школы (?school=...) среди открытых вкладок Kundelik.kz — берётся
// со страницы расписания, на которую просим учителя зайти перед подключением.
// Доступ к url вкладок даёт host_permissions на *.kundelik.kz в manifest.json,
// отдельное разрешение "tabs" для этого не требуется.
function findSchoolId() {
  return new Promise((resolve) => {
    chrome.tabs.query({ url: "*://*.kundelik.kz/*" }, (tabs) => {
      for (const tab of tabs) {
        const m = (tab.url || "").match(/[?&]school=(\d+)/);
        if (m) {
          resolve(m[1]);
          return;
        }
      }
      resolve(null);
    });
  });
}

btn.addEventListener("click", async () => {
  const code = codeEl.value.trim();
  if (!code) {
    setStatus("Введите код из бота.", "err");
    return;
  }

  btn.disabled = true;
  setStatus("Ищу сессию Kundelik.kz...");

  chrome.cookies.get({ url: "https://kundelik.kz", name: "QundelikAuth_a" }, async (cookie) => {
    if (!cookie) {
      setStatus("Вы не вошли в Kundelik.kz. Сначала залогиньтесь на сайте в этом браузере.", "err");
      btn.disabled = false;
      return;
    }

    const school = await findSchoolId();
    setStatus("Сессия найдена, отправляю боту...");
    try {
      const resp = await fetch(`${BACKEND_URL}/pair`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, cookie: cookie.value, school }),
      });
      const data = await resp.json();
      if (resp.ok && data.ok) {
        setStatus(
          school
            ? "Готово! Бот подключён к вашей сессии Kundelik.kz."
            : "Подключено, но не нашла школу — откройте страницу «Расписание» в Kundelik.kz и подключитесь ещё раз, чтобы бот мог его загружать.",
          "ok"
        );
      } else {
        setStatus(data.error || "Не удалось подключить — проверьте код.", "err");
      }
    } catch (e) {
      setStatus("Не удалось связаться с сервером бота.", "err");
    }
    btn.disabled = false;
  });
});
