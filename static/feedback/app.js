"use strict";

// Progressive enhancement: every form and link works without JavaScript.
document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.querySelector("[data-menu-toggle]");
  const sidebar = document.querySelector("#sidebar");
  const mobile = window.matchMedia("(max-width: 800px)");
  const setMenu = (open, moveFocus = false) => {
    open = mobile.matches && open;
    if (!open && sidebar?.contains(document.activeElement)) toggle?.focus();
    document.body.classList.toggle("menu-open", open);
    if (toggle) {
      toggle.setAttribute("aria-expanded", String(open));
      toggle.setAttribute("aria-label", open ? "Закрыть меню" : "Открыть меню");
    }
    if (sidebar) {
      sidebar.inert = mobile.matches && !open;
      if (mobile.matches && !open) sidebar.setAttribute("aria-hidden", "true");
      else sidebar.removeAttribute("aria-hidden");
      if (open && moveFocus) sidebar.querySelector("a, button")?.focus();
    }
  };
  setMenu(false);
  mobile.addEventListener("change", () => setMenu(false));
  toggle?.addEventListener("click", () => setMenu(!document.body.classList.contains("menu-open"), true));
  document.querySelector("[data-close-menu]")?.addEventListener("click", () => setMenu(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setMenu(false);
    if (event.key === "Tab" && mobile.matches && document.body.classList.contains("menu-open") && sidebar) {
      const focusable = [...sidebar.querySelectorAll('a[href], button:not([disabled])')];
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
  document.querySelectorAll("[data-canonical-search]").forEach((input) => {
    const select = document.querySelector("#canonical-ticket");
    const status = document.querySelector("[data-canonical-status]");
    if (!select || !status) return;
    let timer, controller;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      controller?.abort();
      timer = setTimeout(async () => {
        controller = new AbortController();
        status.textContent = "Поиск…";
        try {
          const url = new URL(input.dataset.endpoint, window.location.origin);
          url.searchParams.set("q", input.value);
          const response = await fetch(url, {headers: {Accept: "application/json"}, signal: controller.signal});
          if (!response.ok) throw new Error("search unavailable");
          const data = await response.json();
          select.replaceChildren(new Option("Выберите обращение", ""));
          for (const item of data.results) select.add(new Option(item.label, item.id));
          status.textContent = `Найдено: ${data.count}. ${data.has_next ? "Уточните поиск или откройте полный список." : "Выберите основное обращение."}`;
        } catch (error) {
          if (error.name !== "AbortError") status.textContent = "Поиск временно недоступен. Откройте полный список по ссылке ниже.";
        }
      }, 250);
    });
  });
  // Live "similar requests" hints on the create form. Results come from the
  // user's own access scope; the form submits normally without them.
  const hints = document.querySelector("[data-similar-hints]");
  const description = document.querySelector("#id_description");
  if (hints && description) {
    const list = hints.querySelector("[data-similar-list]");
    const solution = document.querySelector("#id_proposed_solution");
    let timer, controller, lastText = "";
    const render = (results) => {
      list.replaceChildren(...results.map((item) => {
        const li = document.createElement("li");
        const link = document.createElement("a");
        link.href = item.url;
        link.target = "_blank";
        link.rel = "noopener";
        const number = document.createElement("span");
        number.className = "similar-number";
        number.textContent = item.number;
        const title = document.createElement("span");
        title.className = "similar-title";
        title.textContent = item.title;
        link.append(number, title);
        const badge = document.createElement("span");
        badge.className = `status-badge status-${item.status}`;
        badge.append(document.createElement("i"), item.status_label);
        li.append(link, badge);
        return li;
      }));
      hints.hidden = results.length === 0;
    };
    const lookup = () => {
      const text = `${description.value} ${solution?.value || ""}`.trim();
      if (text === lastText) return;
      lastText = text;
      clearTimeout(timer);
      controller?.abort();
      if (text.length < 20) { render([]); return; }
      timer = setTimeout(async () => {
        controller = new AbortController();
        try {
          const url = new URL(hints.dataset.endpoint, window.location.origin);
          url.searchParams.set("text", text.slice(0, 2000));
          const response = await fetch(url, {headers: {Accept: "application/json"}, signal: controller.signal});
          if (!response.ok) throw new Error("similar unavailable");
          render((await response.json()).results);
        } catch (error) {
          if (error.name !== "AbortError") render([]);
        }
      }, 500);
    };
    description.addEventListener("input", lookup);
    solution?.addEventListener("input", lookup);
    lookup();
  }
  // Show which files are queued on the visually compact comment picker.
  document.querySelectorAll("[data-file-input]").forEach((input) => {
    const note = input.parentElement?.querySelector("[data-file-names]");
    if (!note) return;
    const defaultText = note.textContent;
    input.addEventListener("change", () => {
      const names = [...input.files].map((file) => file.name);
      note.textContent = names.length ? `Выбрано: ${names.join(", ")}` : defaultText;
      note.classList.toggle("has-files", names.length > 0);
    });
  });
  document.querySelectorAll("[data-dismiss-alert]").forEach((button) => {
    button.addEventListener("click", () => button.closest(".alert")?.remove());
  });
  document.querySelectorAll("form[data-submit-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (form.dataset.submitting === "true") {
        event.preventDefault();
        return;
      }
      form.dataset.submitting = "true";
      form.setAttribute("aria-busy", "true");
      const button = event.submitter;
      if (button) {
        button.classList.add("is-submitting");
        button.setAttribute("aria-disabled", "true");
      }
    });
  });
  // A history-restored page must allow a fresh action, keeping its original key.
  window.addEventListener("pageshow", () => {
    document.querySelectorAll("form[data-submitting]").forEach((form) => {
      delete form.dataset.submitting;
      form.removeAttribute("aria-busy");
      form.querySelectorAll(".is-submitting").forEach((button) => {
        button.classList.remove("is-submitting");
        button.removeAttribute("aria-disabled");
      });
    });
  });
});

// Ticket "next step" buttons: one action form open at a time. Without JS the
// buttons are #anchors and CSS :target opens the matching form.
document.addEventListener("DOMContentLoaded", () => {
  const toggles = [...document.querySelectorAll("[data-action-toggle]")];
  if (!toggles.length) return;
  const panels = [...document.querySelectorAll("[data-action-panel]")];
  document.querySelector("#next-step")?.classList.add("is-enhanced");
  const show = (id, focus) => {
    panels.forEach((panel) => panel.classList.toggle("is-open", panel.id === id));
    toggles.forEach((toggle) => toggle.setAttribute("aria-expanded", String(toggle.getAttribute("aria-controls") === id)));
    const panel = id && document.getElementById(id);
    if (panel && focus) {
      panel.querySelector("textarea, input:not([type=hidden]), select")?.focus({preventScroll: true});
      panel.scrollIntoView({block: "nearest", behavior: "smooth"});
    }
  };
  toggles.forEach((toggle) => {
    toggle.addEventListener("click", (event) => {
      event.preventDefault();
      const id = toggle.getAttribute("aria-controls");
      show(toggle.getAttribute("aria-expanded") === "true" ? null : id, true);
    });
    toggle.addEventListener("keydown", (event) => {
      if (event.key === " ") { event.preventDefault(); toggle.click(); }
    });
  });
  document.querySelectorAll("[data-action-close]").forEach((close) => {
    close.addEventListener("click", (event) => {
      event.preventDefault();
      const id = close.closest("[data-action-panel]")?.id;
      show(null);
      toggles.find((toggle) => toggle.getAttribute("aria-controls") === id)?.focus();
    });
  });
  const fromHash = panels.find((panel) => "#" + panel.id === window.location.hash);
  if (fromHash) show(fromHash.id, false);
});
