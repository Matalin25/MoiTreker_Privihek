/* HabitFlow — клиентская логика: тема, отметки, календарь, уведомления. */
(function () {
  "use strict";

  // ----- Тема -----
  var saved = localStorage.getItem("hf-theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);

  var themeBtn = document.getElementById("theme-toggle");
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var cur = document.documentElement.getAttribute("data-theme") || "light";
      var next = cur === "light" ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("hf-theme", next);
    });
  }

  // ----- Отметка привычки -----
  function toggleHabit(habitId, dateStr) {
    return fetch("/api/habits/" + habitId + "/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ date: dateStr || null })
    }).then(function (r) { return r.json(); });
  }

  // ----- Всплывающие уведомления (цитаты, бейджи) -----
  function toast(message, kind) {
    var host = document.getElementById("toast-host");
    if (!host) return;
    var el = document.createElement("div");
    el.className = "toast" + (kind ? " toast-" + kind : "");
    el.textContent = message;
    host.appendChild(el);
    setTimeout(function () { el.classList.add("show"); }, 10);
    setTimeout(function () {
      el.classList.remove("show");
      setTimeout(function () { el.remove(); }, 300);
    }, 4200);
  }

  function celebrate(res) {
    if (res.quote) toast(res.quote, "quote");
    (res.new_badges || []).forEach(function (b, i) {
      setTimeout(function () {
        toast("Новая награда: " + b.icon + " " + b.title + " — " + b.desc + "!", "badge");
      }, 350 * (i + 1));
    });
  }

  function refreshProgress() {
    var cardChecks = document.querySelectorAll(".habit-card[data-habit] .check");
    var ring = document.querySelector(".progress-ring span");
    if (ring && cardChecks.length) {
      var done = 0;
      cardChecks.forEach(function (c) { if (c.classList.contains("checked")) done++; });
      var p = Math.round((done / cardChecks.length) * 100);
      ring.textContent = p + "%";
      document.querySelector(".progress-ring").style.setProperty("--p", p);
    }
    var wc = document.querySelectorAll(".widget-row");
    if (wc.length) {
      var wdone = 0;
      wc.forEach(function (row) {
        if (row.querySelector(".check.checked")) wdone++;
      });
      var wr = document.querySelector(".widget-ring span");
      if (wr) wr.textContent = wdone + "/" + wc.length;
      var wring = document.querySelector(".widget-ring");
      if (wring) wring.style.setProperty("--p", Math.round((wdone / wc.length) * 100));
    }
  }

  document.querySelectorAll(".check[data-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var id = btn.getAttribute("data-toggle");
      toggleHabit(id).then(function (res) {
        if (res.error) return;
        btn.classList.toggle("checked", res.done);
        btn.textContent = res.done ? "✓" : "";
        var card = btn.closest(".habit-card, .widget-row");
        if (card) card.classList.toggle("is-done", res.done);
        var streak = document.querySelector('[data-streak="' + id + '"]');
        if (streak) streak.textContent = "🔥 " + res.streak;
        if (res.done) celebrate(res);
        refreshProgress();
      });
    });
  });

  // ----- Календарь -----
  document.querySelectorAll(".cal-cell[data-date]").forEach(function (cell) {
    if (cell.classList.contains("future")) return;
    cell.addEventListener("click", function () {
      var id = cell.getAttribute("data-habit");
      var d = cell.getAttribute("data-date");
      toggleHabit(id, d).then(function (res) {
        if (res.error) return;
        cell.classList.toggle("done", res.done);
        var s = document.querySelector(".stat-value");
        if (s) s.textContent = "🔥 " + res.streak;
        if (res.done) celebrate(res);
      });
    });
    cell.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); cell.click(); }
    });
  });

  // ----- Service worker + уведомления -----
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
  }

  function setStatus(msg) {
    var el = document.getElementById("notif-status");
    if (el) el.textContent = msg;
  }

  var enableBtn = document.getElementById("enable-notifications");
  if (enableBtn) {
    enableBtn.addEventListener("click", function () {
      if (!("Notification" in window)) { setStatus("Браузер не поддерживает уведомления."); return; }
      Notification.requestPermission().then(function (perm) {
        setStatus(perm === "granted" ? "Уведомления разрешены." : "Уведомления запрещены.");
      });
    });
  }

  function showNotification(title, body) {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    if (navigator.serviceWorker && navigator.serviceWorker.ready) {
      navigator.serviceWorker.ready.then(function (reg) {
        reg.showNotification(title, { body: body, icon: "/static/icon.svg", tag: "habitflow" });
      }).catch(function () { new Notification(title, { body: body }); });
    } else {
      new Notification(title, { body: body });
    }
  }

  var testBtn = document.getElementById("test-notification");
  if (testBtn) {
    testBtn.addEventListener("click", function () {
      showNotification("HabitFlow", "Это тестовое уведомление. Всё работает!");
    });
  }

  // Периодическая проверка напоминаний (пока вкладка открыта).
  function checkReminders() {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    fetch("/api/reminders").then(function (r) { return r.json(); }).then(function (data) {
      if (!data.enabled || !data.pending.length) return;
      var now = new Date();
      var hm = ("0" + now.getHours()).slice(-2) + ":" + ("0" + now.getMinutes()).slice(-2);
      if (hm < data.time) return;
      var key = "hf-notified-" + data.date;
      if (localStorage.getItem(key)) return;
      var names = data.pending.map(function (h) { return h.icon + " " + h.title; }).join(", ");
      showNotification("Не забудьте отметить привычки!",
        "Осталось: " + data.pending.length + " — " + names);
      localStorage.setItem(key, "1");
    }).catch(function () {});
  }

  checkReminders();
  setInterval(checkReminders, 60000);

  // ----- Соцмодель: поделиться достижением -----
  document.querySelectorAll(".share-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var id = btn.getAttribute("data-habit");
      fetch("/api/habits/" + id + "/share").then(function (r) { return r.json(); }).then(function (data) {
        if (data.error) return;
        var text = data.text;
        if (navigator.share) {
          navigator.share({ title: "HabitFlow", text: text }).catch(function () {});
          return;
        }
        if (navigator.clipboard) {
          navigator.clipboard.writeText(text).then(function () {
            toast("Скопировано: " + text, "badge");
          }).catch(function () { toast(text, "badge"); });
        } else {
          toast(text, "badge");
        }
      });
    });
  });

  // ----- Копирование API-ключа / ссылки календаря -----
  function bindCopy(buttonId, sourceId) {
    var btn = document.getElementById(buttonId);
    var src = document.getElementById(sourceId);
    if (!btn || !src) return;
    btn.addEventListener("click", function () {
      var text = src.textContent.trim();
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(function () { toast("Скопировано", "badge"); });
      } else {
        toast(text, "badge");
      }
    });
  }
  bindCopy("copy-key", "api-key");
  bindCopy("copy-ics", "ics-url");
})();
