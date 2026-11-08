(function () {
  "use strict";

  var state = {
    endpoint: "schedule",
    section: "schedule",
    subfilter: "all",
    payload: null,
    selectedCard: null
  };

  var groupBadge = document.getElementById("group-badge");
  var demoBadge = document.getElementById("demo-badge");
  var todayBanner = document.getElementById("today-banner");
  var todayText = document.getElementById("today-text");
  var mainTabs = document.getElementById("main-tabs");
  var subfilters = document.getElementById("subfilters");

  var stateLoading = document.getElementById("state-loading");
  var stateError = document.getElementById("state-error");
  var errorTitle = document.getElementById("error-title");
  var errorMessage = document.getElementById("error-message");
  var retryButton = document.getElementById("retry-button");

  var viewList = document.getElementById("view-list");
  var cardsContainer = document.getElementById("cards-container");

  var viewDetail = document.getElementById("view-detail");
  var backButton = document.getElementById("back-button");
  var detailBadge = document.getElementById("detail-badge");
  var detailTitle = document.getElementById("detail-title");
  var detailSubtitle = document.getElementById("detail-subtitle");
  var detailMeta = document.getElementById("detail-meta");
  var detailTags = document.getElementById("detail-tags");
  var detailBody = document.getElementById("detail-body");
  var detailReportBox = document.getElementById("detail-report-box");
  var detailReportBody = document.getElementById("detail-report-body");
  var detailReportLink = document.getElementById("detail-report-link");
  var detailLink = document.getElementById("detail-link");
  var detailFilesBox = document.getElementById("detail-files-box");
  var detailFilesList = document.getElementById("detail-files-list");

  function displayTitle(value) {
    var title = (value || "").replace(/^ДЕМО\s*[—·-]\s*/, "");
    return title.charAt(0).toLocaleUpperCase("ru") + title.slice(1);
  }

  function clearChildren(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function showLoading() {
    stateLoading.hidden = false;
    stateError.hidden = true;
    viewList.hidden = true;
    viewDetail.hidden = true;
  }

  function showError(status, message) {
    stateLoading.hidden = true;
    viewList.hidden = true;
    viewDetail.hidden = true;
    stateError.hidden = false;

    errorTitle.textContent = "Не удалось загрузить материалы";
    errorMessage.textContent = message || "Проверьте соединение и повторите попытку. Вход не требуется.";
    retryButton.hidden = false;
  }

  function stopMedia() {
    document.querySelectorAll("video").forEach(function(video){ video.pause(); });
  }

  function closeDetailView() {
    stopMedia();
    state.selectedCard = null;
    viewDetail.hidden = true;
    viewList.hidden = false;
    if (state.payload) renderCards(state.payload);
  }

  function openDetailView(card) {
    stopMedia();
    state.selectedCard = card;
    viewList.hidden = true;
    stateError.hidden = true;
    stateLoading.hidden = true;
    viewDetail.hidden = false;

    if (card.badge) {
      detailBadge.textContent = card.badge;
      detailBadge.hidden = false;
    } else {
      detailBadge.hidden = true;
    }

    detailTitle.textContent = displayTitle(card.title);
    detailTitle.focus();
    detailSubtitle.textContent = card.subtitle || "";

    if (card.meta) {
      detailMeta.textContent = card.meta;
      detailMeta.hidden = false;
    } else {
      detailMeta.hidden = true;
    }

    clearChildren(detailTags);
    if (Array.isArray(card.tags) && card.tags.length > 0) {
      card.tags.forEach(function (t) {
        var chip = document.createElement("span");
        chip.className = "tag-chip";
        chip.textContent = t;
        detailTags.appendChild(chip);
      });
      detailTags.hidden = false;
    } else {
      detailTags.hidden = true;
    }

    detailBody.textContent = card.body || card.preview || "";
    var mediaBox = document.getElementById("detail-media");
    clearChildren(mediaBox);
    (card.media || []).forEach(function(media) {
      var el = document.createElement(media.type === "video" ? "video" : "img");
      el.src = media.src;
      if (media.type === "video") {
        el.controls = true; el.playsInline = true; el.preload = "metadata";
        el.poster = "./assets/dungeon.png";
      } else { el.alt = media.alt || "ДЕМО — иллюстрация мероприятия"; el.loading = "lazy"; }
      el.addEventListener("error", function(){
        var hint = document.createElement("p"); hint.textContent = "Медиа не загрузилось. Откройте карточку ещё раз.";
        mediaBox.appendChild(hint);
      });
      mediaBox.appendChild(el);
    });
    var joinButton = document.getElementById("join-button");
    joinButton.hidden = !card.joinable;
    joinButton.textContent = card.joined ? "Отменить пробную запись" : "Записаться в примере";
    joinButton.onclick = function(){
      card.joined = !card.joined;
      card.count = (card.count || 0) + (card.joined ? 1 : -1);
      card.badge = card.joined ? "Вы записаны (демо)" : "Запись открыта";
      document.getElementById("demo-action-status").textContent = card.joined ? "ДЕМО — запись только в этой вкладке. Уведомления не отправляются." : "ДЕМО — участие отменено в этой вкладке.";
      joinButton.textContent = card.joined ? "Отменить пробную запись" : "Записаться в примере";
      detailBadge.textContent = card.badge;
    };
    document.getElementById("demo-action-status").textContent = "";

    if (card.report_published && card.report_body) {
      detailReportBody.textContent = card.report_body;
      if (card.report_link && card.report_link.indexOf("https://") === 0) {
        detailReportLink.href = card.report_link;
        detailReportLink.hidden = false;
      } else {
        detailReportLink.hidden = true;
      }
      detailReportBox.hidden = false;
    } else {
      detailReportBox.hidden = true;
    }

    if (card.link && card.link.indexOf("https://") === 0) {
      detailLink.href = card.link;
      detailLink.hidden = false;
    } else {
      detailLink.hidden = true;
    }

    clearChildren(detailFilesList);
    if (Array.isArray(card.files) && card.files.length > 0) {
      card.files.forEach(function (fname) {
        var li = document.createElement("li");
        li.className = "file-item";
        var link = document.createElement("a");
        link.textContent = fname; link.href = "./assets/demo-task.txt"; link.download = "demo-task.txt";
        li.appendChild(link);
        detailFilesList.appendChild(li);
      });
      detailFilesBox.hidden = false;
    } else {
      detailFilesBox.hidden = true;
    }


  }

  function renderSubfilters(payload) {
    clearChildren(subfilters);
    if (state.section === "events") {
      subfilters.hidden = false;
      [
        { key: "upcoming", label: " Предстоящие" },
        { key: "history", label: " Как это было" }
      ].forEach(function (item) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "chip-button" + (state.subfilter === item.key ? " is-active" : "");
        btn.textContent = item.label;
        btn.addEventListener("click", function () {
          state.subfilter = item.key;
          closeDetailView();
          renderSubfilters(payload);
          renderCards(payload);
        });
        subfilters.appendChild(btn);
      });
      return;
    }

    if (state.section === "useful" && Array.isArray(payload.categories) && payload.categories.length > 0) {
      subfilters.hidden = false;
      var allItems = [{ key: "all", label: "Все" }].concat(payload.categories);
      allItems.forEach(function (cat) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "chip-button" + (state.subfilter === cat.key ? " is-active" : "");
        btn.textContent = cat.label;
        btn.addEventListener("click", function () {
          state.subfilter = cat.key;
          closeDetailView();
          renderSubfilters(payload);
          renderCards(payload);
        });
        subfilters.appendChild(btn);
      });
      return;
    }

    subfilters.hidden = true;
  }

  function getActiveCards(payload) {
    if (!payload) {
      return [];
    }
    if (state.section === "events" && state.subfilter === "history") {
      return Array.isArray(payload.history) ? payload.history : [];
    }
    var baseCards = Array.isArray(payload.cards) ? payload.cards : [];
    if (state.section === "useful" && state.subfilter !== "all") {
      return baseCards.filter(function (c) {
        return c.category === state.subfilter;
      });
    }
    if (baseCards.length > 0) {
      return baseCards;
    }
    if (Array.isArray(payload.items)) {
      return payload.items.map(function (text, idx) {
        var lines = String(text).split("\n");
        var first = lines[0] || "Запись";
        var rest = lines.slice(1).join("\n") || first;
        return {
          id: "item-" + idx,
          title: first,
          subtitle: payload.group || "",
          badge: "",
          preview: rest,
          body: String(text)
        };
      });
    }
    return [];
  }

  function renderCards(payload) {
    clearChildren(cardsContainer);
    var cards = getActiveCards(payload);

    if (cards.length === 0) {
      var empty = document.createElement("div");
      empty.className = "empty-card";
      empty.textContent = "В этом разделе пока нет опубликованных записей.";
      cardsContainer.appendChild(empty);
      return;
    }

    cards.forEach(function (card) {
      var article = document.createElement("article");
      article.className = "item-card";
      article.tabIndex = 0;

      var topRow = document.createElement("div");
      topRow.className = "item-card-top";

      var title = document.createElement("h2");
      title.className = "item-card-title";
      title.textContent = displayTitle(card.title);
      topRow.appendChild(title);

      if (card.badge) {
        var badge = document.createElement("span");
        badge.className = "pill";
        badge.textContent = card.badge;
        topRow.appendChild(badge);
      }

      article.appendChild(topRow);


      if (card.subtitle) {
        var sub = document.createElement("p");
        sub.className = "item-card-subtitle";
        sub.textContent = card.subtitle;
        article.appendChild(sub);
      }

      var preview = document.createElement("p");
      preview.className = "item-card-preview";
      preview.textContent = card.preview || card.body || "";
      article.appendChild(preview);

      var footer = document.createElement("div");
      footer.className = "item-card-footer";
      var openHint = document.createElement("span");
      openHint.className = "open-hint";
      openHint.textContent = "Подробнее";
      footer.appendChild(openHint);
      article.appendChild(footer);

      article.addEventListener("click", function () {
        openDetailView(card);
      });
      article.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          openDetailView(card);
        }
      });

      cardsContainer.appendChild(article);
    });
  }

  function renderPayload(payload) {
    state.payload = payload;
    stateLoading.hidden = true;
    stateError.hidden = true;
    viewDetail.hidden = true;
    viewList.hidden = false;
    state.selectedCard = null;

    demoBadge.hidden = !payload.demo;
    if (payload.group) {
      groupBadge.textContent = "Группа " + payload.group;
    } else {
      groupBadge.textContent = "Группа не выбрана";
    }

    if (payload.summary) {
      todayText.textContent = payload.summary;
      todayBanner.hidden = false;
    } else {
      todayBanner.hidden = true;
    }

    renderSubfilters(payload);
    renderCards(payload);

  }

  var requestVersion = 0;
  var pendingRequest = null;
  var demoDataset = null;

  function loadSection(endpoint, sectionName) {
    var labels = {
      schedule: ["Расписание", "Время занятий, предметы и изменения."],
      homework: ["Задания", "Общие задания и личные учебные примеры."],
      events: ["Мероприятия", "Предстоящие события и опубликованные итоги."],
      useful: ["Полезное", "Контакты, кабинеты и учебные материалы."]
    };
    document.getElementById("section-title").textContent = labels[sectionName][0];
    document.getElementById("section-description").textContent = labels[sectionName][1];
    var version = ++requestVersion;
    if (pendingRequest) pendingRequest.abort();
    pendingRequest = new AbortController();

    state.endpoint = endpoint;
    state.section = sectionName;
    state.selectedCard = null;
    state.subfilter = sectionName === "events" ? "upcoming" : "all";
    stopMedia();
    showLoading();

    var request = demoDataset ? Promise.resolve(demoDataset) : fetch("./data/demo.json", {method:"GET", signal:pendingRequest.signal, cache:"no-store"})
      .then(function (response) {
        if (!response.ok) throw new Error("Не удалось загрузить демонстрационные данные.");
        return response.json();
      })
      ;
    request.then(function (dataset) {
        demoDataset = dataset;
        var payload = dataset.sections[sectionName];
        if (!payload) throw new Error("Раздел временно недоступен.");
        return {ok:true,status:200,data:payload};
      })
      .then(function (res) {
        if (version !== requestVersion) return;
        if (!res.ok) {
          showError(res.status, res.data && res.data.error ? res.data.error : "Ошибка доступа.");
          return;
        }
        renderPayload(res.data);
      })
      .catch(function (error) {
        if (version !== requestVersion || error.name === "AbortError") return;
        showError(503, "Не удалось связаться с сервером. Проверьте подключение к сети.");
      });
  }

  mainTabs.addEventListener("click", function (ev) {
    var btn = ev.target.closest(".tab-button");
    if (!btn) {
      return;
    }
    stopMedia();
    var buttons = mainTabs.querySelectorAll(".tab-button");
    buttons.forEach(function (b) {
      b.classList.toggle("is-active", b === btn);
      b.setAttribute("aria-pressed", b === btn ? "true" : "false");
    });
    loadSection(btn.getAttribute("data-endpoint"), btn.getAttribute("data-section"));
  });

  backButton.addEventListener("click", function () {
    closeDetailView();
  });

  retryButton.addEventListener("click", function () {
    loadSection(state.endpoint, state.section);
  });

  document.getElementById("theme-toggle").addEventListener("click", function(){
    var dark = document.documentElement.dataset.theme === "dark" ||
      (!document.documentElement.dataset.theme && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    this.textContent = dark ? "Тёмная тема" : "Светлая тема";
  });
  document.addEventListener("keydown", function(event){ if(event.key === "Escape" && state.selectedCard) closeDetailView(); });
  loadSection(state.endpoint, state.section);
})();