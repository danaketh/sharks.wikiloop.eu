(() => {
  "use strict";

  const schema = JSON.parse(document.getElementById("cms-editor-schema").textContent);
  const initial = JSON.parse(document.getElementById("cms-editor-document").textContent);
  const endpoints = JSON.parse(document.getElementById("cms-editor-endpoints").textContent);
  const media = JSON.parse(document.getElementById("cms-editor-media").textContent);
  const blockTypes = new Map(schema.blocks.map((item) => [item.key, item]));
  const state = structuredClone(initial);

  const fieldsRoot = document.getElementById("cms-editor-fields");
  const errorsRoot = document.getElementById("cms-editor-errors");
  const readinessRoot = document.getElementById("cms-editor-readiness");
  const statusRoot = document.getElementById("cms-editor-status");
  const revisionRoot = document.getElementById("cms-editor-revision");
  const saveButton = document.getElementById("cms-editor-save");
  const publishButton = document.getElementById("cms-editor-publish");

  const PICKER_DEBOUNCE_MS = 250;
  const PICKER_LIMIT = 10;

  // Latest server publish_readiness ({ready, problems}); null for new content.
  let readiness = endpoints.document ? {ready: null, pending: true, problems: []} : null;
  let readinessRequest = 0;
  let publishing = false;
  let nextFieldId = 0;
  // Titles/status of references chosen in this session, keyed by content id.
  const referenceLabels = new Map();
  // Temporary object URLs for blobs uploaded in this session, keyed by blob id.
  const blobPreviews = new Map();

  function getPath(target, path) {
    return path.reduce((value, segment) => value == null ? undefined : value[segment], target);
  }

  function setPath(target, path, value) {
    let current = target;
    path.slice(0, -1).forEach((segment) => {
      if (!current[segment] || typeof current[segment] !== "object") current[segment] = {};
      current = current[segment];
    });
    current[path[path.length - 1]] = value;
  }

  function element(tag, attrs = {}, text = "") {
    const node = document.createElement(tag);
    Object.entries(attrs).forEach(([key, value]) => {
      if (key === "class") node.className = value;
      else if (key === "disabled") node.disabled = Boolean(value);
      else node.setAttribute(key, String(value));
    });
    if (text) node.textContent = text;
    return node;
  }

  function fieldId(prefix) {
    nextFieldId += 1;
    return `${prefix}-${nextFieldId}`;
  }

  function fieldWrapper(field, className, inputId) {
    const wrapper = element("div", {class: className});
    wrapper.appendChild(element("label", {for: inputId}, field.label));
    return wrapper;
  }

  function appendHelp(field, wrapper) {
    if (field.help_text) wrapper.appendChild(element("small", {class: "cms-help"}, field.help_text));
  }

  function errorText(error) {
    const detail = error && typeof error.detail === "string" ? error.detail : "Request failed.";
    return error && typeof error.code === "string" ? `${detail} (${error.code})` : detail;
  }

  function linkId(value, key) {
    return value && typeof value === "object" && typeof value[key] === "string" ? value[key] : null;
  }

  function renderScalar(field, target, parent, className = "cms-editor-field") {
    const widget = {
      markdown: renderMarkdown,
      reference: renderReference,
      blob: renderBlob,
    }[field.input_kind];
    if (widget) {
      widget(field, target, parent, className);
      return;
    }

    const wrapper = element("div", {class: className});
    const label = element("label", {}, field.label);
    wrapper.appendChild(label);
    const current = getPath(target, field.path);
    let input;

    if (field.input_kind === "textarea") {
      input = element("textarea", {rows: 6});
      input.value = current ?? "";
    } else if (field.input_kind === "boolean") {
      input = element("input", {type: "checkbox"});
      input.checked = Boolean(current);
    } else if (field.input_kind === "choice" || field.input_kind === "multi_choice") {
      input = element("select", field.input_kind === "multi_choice" ? {multiple: true} : {});
      field.choices.forEach((choice) => {
        const option = element("option", {value: choice.value}, choice.label);
        if (field.input_kind === "multi_choice") {
          option.selected = Array.isArray(current) && current.includes(choice.value);
        } else {
          option.selected = current === choice.value;
        }
        input.appendChild(option);
      });
    } else {
      const type = {
        integer: "number",
        decimal: "number",
        date: "date",
        datetime: "datetime-local",
        email: "email",
        url: "url",
      }[field.input_kind] || "text";
      input = element("input", {type});
      input.value = current ?? "";
      if (field.placeholder) input.placeholder = field.placeholder;
      if (field.input_kind === "decimal") input.step = "any";
    }

    input.disabled = Boolean(field.read_only);
    if (field.required) input.required = true;
    input.addEventListener("change", () => {
      let value;
      if (field.input_kind === "boolean") value = input.checked;
      else if (field.input_kind === "integer") value = input.value === "" ? null : Number.parseInt(input.value, 10);
      else if (field.input_kind === "decimal") value = input.value === "" ? null : Number.parseFloat(input.value);
      else if (field.input_kind === "multi_choice") value = [...input.selectedOptions].map((option) => option.value);
      else value = input.value;
      setPath(target, field.path, value);
    });
    wrapper.appendChild(input);
    if (field.help_text) wrapper.appendChild(element("small", {class: "cms-help"}, field.help_text));
    parent.appendChild(wrapper);
  }

  function renderMarkdown(field, target, parent, className) {
    const inputId = fieldId("cms-markdown");
    const previewId = `${inputId}-preview`;
    const wrapper = fieldWrapper(field, className, inputId);
    const original = getPath(target, field.path);
    const input = element("textarea", {id: inputId, rows: 10, class: "cms-markdown-input"});
    input.value = original ?? "";
    input.disabled = Boolean(field.read_only);
    if (field.required) input.required = true;
    if (field.placeholder) input.placeholder = field.placeholder;
    input.addEventListener("input", () => {
      // Keep an originally-null optional value null while it stays blank.
      setPath(target, field.path, input.value === "" && original == null ? null : input.value);
    });

    const toolbar = element("div", {class: "cms-widget-toolbar"});
    const toggle = element("button", {
      type: "button",
      class: "button",
      "aria-controls": previewId,
      "aria-expanded": "false",
    }, "Preview");
    const message = element("span", {class: "cms-widget-message", role: "status"});
    toolbar.append(toggle, message);
    const preview = element("div", {id: previewId, class: "cms-markdown-preview"});
    preview.hidden = true;
    let previewRequest = 0;

    function showSource() {
      previewRequest += 1;
      preview.hidden = true;
      preview.replaceChildren();
      input.hidden = false;
      toggle.textContent = "Preview";
      toggle.setAttribute("aria-expanded", "false");
    }

    toggle.addEventListener("click", async () => {
      if (!preview.hidden) {
        showSource();
        return;
      }
      previewRequest += 1;
      const mine = previewRequest;
      toggle.disabled = true;
      message.textContent = "Rendering preview…";
      try {
        const payload = await send(endpoints.markdown_preview, {source: input.value});
        if (mine !== previewRequest) return;
        if (!payload || typeof payload.html !== "string") throw {detail: "Preview response was invalid."};
        // Sanitized server-side by the Markdown preview endpoint; the only HTML sink.
        preview.innerHTML = payload.html;
        preview.hidden = false;
        input.hidden = true;
        toggle.textContent = "Edit";
        toggle.setAttribute("aria-expanded", "true");
        message.textContent = "";
      } catch (error) {
        if (mine === previewRequest) message.textContent = errorText(error);
      } finally {
        toggle.disabled = false;
      }
    });

    wrapper.append(input, preview, toolbar);
    appendHelp(field, wrapper);
    parent.appendChild(wrapper);
  }

  function referenceSummary(id) {
    const known = referenceLabels.get(id);
    if (!known) return `Content ${id}`;
    return `${known.title || "Untitled"} · ${known.is_published ? "Published" : "Draft"}`;
  }

  function renderReference(field, target, parent, className) {
    const searchId = fieldId("cms-reference");
    const wrapper = fieldWrapper(field, className, searchId);
    const many = Boolean(field.reference_many);
    const readOnly = Boolean(field.read_only);
    let current = getPath(target, field.path);
    if (many && !Array.isArray(current)) {
      current = [];
      setPath(target, field.path, current);
    }

    const selected = element("ul", {class: "cms-reference-selected"});
    const values = many ? current : (current == null ? [] : [current]);
    values.forEach((value, index) => {
      const id = linkId(value, "$ref");
      const item = element("li", {class: "cms-reference-item"});
      item.appendChild(element("span", {}, id ? referenceSummary(id) : "Invalid reference"));
      const actions = element("span", {class: "cms-block-actions"});
      if (many) {
        const up = element("button", {
          type: "button", class: "button", "aria-label": "Move up", disabled: readOnly || index === 0,
        }, "↑");
        const down = element("button", {
          type: "button", class: "button", "aria-label": "Move down",
          disabled: readOnly || index === values.length - 1,
        }, "↓");
        const remove = element("button", {type: "button", class: "button", disabled: readOnly}, "Remove");
        up.addEventListener("click", () => {
          [current[index - 1], current[index]] = [current[index], current[index - 1]];
          render();
        });
        down.addEventListener("click", () => {
          [current[index + 1], current[index]] = [current[index], current[index + 1]];
          render();
        });
        remove.addEventListener("click", () => {
          current.splice(index, 1);
          render();
        });
        actions.append(up, down, remove);
      } else {
        const clear = element("button", {type: "button", class: "button", disabled: readOnly}, "Clear");
        clear.addEventListener("click", () => {
          setPath(target, field.path, null);
          render();
        });
        actions.appendChild(clear);
      }
      item.appendChild(actions);
      selected.appendChild(item);
    });
    if (values.length === 0) {
      selected.appendChild(element("li", {class: "cms-reference-empty"}, "Nothing selected."));
    }
    wrapper.appendChild(selected);

    function choose(result) {
      referenceLabels.set(result.id, {title: result.title, is_published: result.is_published});
      const value = {$ref: result.id};
      if (!many) setPath(target, field.path, value);
      else if (!current.some((existing) => linkId(existing, "$ref") === result.id)) current.push(value);
      render();
    }

    const search = element("input", {
      type: "search",
      id: searchId,
      autocomplete: "off",
      placeholder: many ? "Search to add…" : "Search to select…",
      disabled: readOnly,
    });
    const message = element("span", {class: "cms-widget-message", role: "status"});
    const results = element("ul", {class: "cms-reference-results", "aria-label": `${field.label} results`});
    let timer = null;
    let controller = null;
    let searchRequest = 0;

    async function runSearch(text) {
      searchRequest += 1;
      const mine = searchRequest;
      if (controller) controller.abort();
      controller = null;
      if (text === "") {
        results.replaceChildren();
        message.textContent = "";
        return;
      }
      controller = new AbortController();
      const url = new URL(endpoints.picker, window.location.origin);
      field.reference_types.forEach((type) => url.searchParams.append("type", type));
      url.searchParams.set("q", text);
      url.searchParams.set("limit", String(PICKER_LIMIT));
      message.textContent = "Searching…";
      try {
        const payload = await request(url, {signal: controller.signal});
        if (mine !== searchRequest) return;
        results.replaceChildren();
        payload.items.forEach((result) => {
          const parts = [result.title || "Untitled"];
          if (field.reference_types.length > 1) parts.push(result.type);
          parts.push(result.is_published ? "Published" : "Draft");
          const option = element("button", {type: "button", class: "cms-reference-result"}, parts.join(" · "));
          option.addEventListener("click", () => choose(result));
          const row = element("li");
          row.appendChild(option);
          results.appendChild(row);
        });
        message.textContent = payload.items.length
          ? `Showing ${payload.items.length} of ${payload.total_count}.`
          : "No matches.";
      } catch (error) {
        if (mine !== searchRequest || (error && error.name === "AbortError")) return;
        results.replaceChildren();
        message.textContent = errorText(error);
      }
    }

    search.addEventListener("input", () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => runSearch(search.value.trim()), PICKER_DEBOUNCE_MS);
    });
    wrapper.append(search, message, results);

    if (!readOnly && media && field.reference_types.includes(media.type)) {
      const uploadId = fieldId("cms-reference-upload");
      const upload = element("div", {class: "cms-reference-upload"});
      const uploadInput = element("input", {type: "file", id: uploadId, accept: "image/*"});
      const uploadMessage = element("span", {class: "cms-widget-message", role: "status"});
      uploadInput.addEventListener("change", async () => {
        const file = uploadInput.files[0];
        if (!file) return;
        uploadInput.disabled = true;
        uploadMessage.textContent = "Uploading…";
        try {
          const blob = await uploadBlob(file);
          uploadMessage.textContent = "Creating media draft…";
          const data = structuredClone(media.initial_data);
          setPath(data, media.blob_path, {$blob: blob.blob_id});
          const created = await send(endpoints.create, {
            type: media.type,
            schema_version: media.schema_version,
            data,
          });
          choose({id: created.content_id, title: "New media", is_published: false});
        } catch (error) {
          uploadMessage.textContent = errorText(error);
          uploadInput.disabled = false;
          uploadInput.value = "";
        }
      });
      upload.append(element("label", {for: uploadId}, "Upload new"), uploadInput, uploadMessage);
      wrapper.appendChild(upload);
    }

    appendHelp(field, wrapper);
    parent.appendChild(wrapper);
  }

  function renderBlob(field, target, parent, className) {
    const inputId = fieldId("cms-blob");
    const wrapper = fieldWrapper(field, className, inputId);
    const readOnly = Boolean(field.read_only);
    const currentId = linkId(getPath(target, field.path), "$blob");

    const current = element("div", {class: "cms-blob-current"});
    if (currentId) {
      const previewUrl = blobPreviews.get(currentId);
      if (previewUrl) current.appendChild(element("img", {src: previewUrl, alt: "", class: "cms-blob-thumbnail"}));
      current.appendChild(element("span", {}, `Stored file ${currentId}`));
      const remove = element("button", {type: "button", class: "button", disabled: readOnly}, "Remove file");
      remove.addEventListener("click", () => {
        setPath(target, field.path, null);
        render();
      });
      current.appendChild(remove);
    } else {
      current.appendChild(element("span", {}, "No file uploaded."));
    }

    const input = element("input", {type: "file", id: inputId, accept: "image/*", disabled: readOnly});
    const message = element("span", {class: "cms-widget-message", role: "status"});
    input.addEventListener("change", async () => {
      const file = input.files[0];
      if (!file) return;
      input.disabled = true;
      message.textContent = "Uploading…";
      try {
        const blob = await uploadBlob(file);
        const previous = blobPreviews.get(blob.blob_id);
        if (previous) URL.revokeObjectURL(previous);
        blobPreviews.set(blob.blob_id, URL.createObjectURL(file));
        setPath(target, field.path, {$blob: blob.blob_id});
        render();
      } catch (error) {
        message.textContent = errorText(error);
        input.disabled = readOnly;
        input.value = "";
      }
    });

    wrapper.append(current, input, message);
    appendHelp(field, wrapper);
    parent.appendChild(wrapper);
  }

  function releaseUnusedPreviews() {
    if (blobPreviews.size === 0) return;
    const serialized = JSON.stringify(state.data);
    blobPreviews.forEach((url, id) => {
      if (!serialized.includes(id)) {
        URL.revokeObjectURL(url);
        blobPreviews.delete(id);
      }
    });
  }

  function allowedBlockTypes(constraint) {
    if (constraint.allowed_types === null) return schema.blocks;
    return constraint.allowed_types.map((key) => blockTypes.get(key)).filter(Boolean);
  }

  function newBlock(type) {
    const definition = blockTypes.get(type);
    if (!definition) throw new Error(`Unknown block type ${type}`);
    const slots = Object.fromEntries(definition.slots.map((slot) => [slot.name, []]));
    return {
      id: crypto.randomUUID(),
      type,
      version: definition.schema_version,
      data: structuredClone(definition.initial_data),
      slots,
    };
  }

  function renderCollection(collection, constraint, parent, title) {
    const wrapper = element("div", {class: "cms-editor-field"});
    wrapper.appendChild(element("label", {}, title));
    const list = element("div", {class: "cms-block-collection"});

    collection.forEach((block, index) => {
      const definition = blockTypes.get(block.type);
      const blockNode = element("div", {class: "cms-block"});
      const header = element("div", {class: "cms-block-header"});
      header.appendChild(element("strong", {}, definition?.label || block.type));
      const actions = element("div", {class: "cms-block-actions"});
      const up = element("button", {type: "button", class: "button", disabled: index === 0}, "↑");
      const down = element("button", {type: "button", class: "button", disabled: index === collection.length - 1}, "↓");
      const remove = element("button", {
        type: "button",
        class: "button",
        disabled: collection.length <= constraint.min_items,
      }, "Remove");
      up.addEventListener("click", () => {
        [collection[index - 1], collection[index]] = [collection[index], collection[index - 1]];
        render();
      });
      down.addEventListener("click", () => {
        [collection[index + 1], collection[index]] = [collection[index], collection[index + 1]];
        render();
      });
      remove.addEventListener("click", () => {
        collection.splice(index, 1);
        render();
      });
      actions.append(up, down, remove);
      header.appendChild(actions);
      blockNode.appendChild(header);

      if (definition) {
        const blockFields = element("div", {class: "cms-block-fields"});
        definition.fields.forEach((field) => renderScalar(field, block.data, blockFields, "cms-block-field"));
        blockNode.appendChild(blockFields);
        definition.slots.forEach((slot) => {
          if (!block.slots[slot.name]) block.slots[slot.name] = [];
          const slotNode = element("div", {class: "cms-block-slot"});
          renderCollection(block.slots[slot.name], slot.constraint, slotNode, slot.label);
          blockNode.appendChild(slotNode);
        });
      }
      list.appendChild(blockNode);
    });

    wrapper.appendChild(list);
    const options = allowedBlockTypes(constraint).filter((item) => item.editable);
    const canAdd = options.length > 0 && (constraint.max_items === null || collection.length < constraint.max_items);
    if (canAdd) {
      const add = element("div", {class: "cms-collection-add"});
      const select = element("select");
      options.forEach((item) => select.appendChild(element("option", {value: item.key}, item.label)));
      const button = element("button", {type: "button", class: "button"}, "Add block");
      button.addEventListener("click", () => {
        collection.push(newBlock(select.value));
        render();
      });
      add.append(select, button);
      wrapper.appendChild(add);
    }
    parent.appendChild(wrapper);
  }

  function updatePublishButton() {
    const notReady = readiness !== null && (readiness.pending || readiness.ready === false);
    publishButton.disabled = publishing || state.is_new || !state.revision_id || notReady;
  }

  function renderReadiness() {
    readinessRoot.replaceChildren();
    readinessRoot.classList.remove("cms-editor-readiness--ready", "cms-editor-readiness--blocked");
    if (readiness === null) {
      readinessRoot.hidden = true;
      return;
    }
    readinessRoot.hidden = false;
    if (readiness.pending) {
      readinessRoot.appendChild(element("span", {}, "Checking publish readiness…"));
    } else if (readiness.error) {
      readinessRoot.appendChild(element("span", {}, `Publish readiness unavailable: ${readiness.error}`));
    } else if (readiness.ready) {
      readinessRoot.classList.add("cms-editor-readiness--ready");
      readinessRoot.appendChild(element("strong", {}, "Ready to publish."));
    } else {
      readinessRoot.classList.add("cms-editor-readiness--blocked");
      readinessRoot.appendChild(element("strong", {}, "Not ready to publish:"));
      const list = element("ul");
      (readiness.problems || []).forEach((problem) => {
        list.appendChild(element("li", {}, `${problem.path}: ${problem.message}`));
      });
      readinessRoot.appendChild(list);
    }
  }

  async function loadReadiness() {
    if (!endpoints.document) return;
    readinessRequest += 1;
    const mine = readinessRequest;
    readiness = {ready: null, pending: true, problems: []};
    renderReadiness();
    updatePublishButton();
    try {
      const payload = await request(endpoints.document);
      if (mine !== readinessRequest) return;
      const result = payload && payload.publish_readiness;
      readiness = result && typeof result.ready === "boolean"
        ? {ready: result.ready, problems: Array.isArray(result.problems) ? result.problems : []}
        : {ready: null, error: "Response did not include readiness.", problems: []};
    } catch (error) {
      if (mine !== readinessRequest) return;
      // Server-side publish checks stay authoritative, so publishing remains possible.
      readiness = {ready: null, error: errorText(error), problems: []};
    }
    renderReadiness();
    updatePublishButton();
  }

  function render() {
    fieldsRoot.replaceChildren();
    schema.fields.forEach((field) => {
      if (field.constraint) {
        let collection = getPath(state.data, field.path);
        if (!Array.isArray(collection)) {
          collection = [];
          setPath(state.data, field.path, collection);
        }
        renderCollection(collection, field.constraint, fieldsRoot, field.label);
      } else {
        renderScalar(field, state.data, fieldsRoot);
      }
    });
    releaseUnusedPreviews();
    revisionRoot.textContent = state.is_new
      ? "New content"
      : `Revision ${state.revision_number} · content version ${state.content_version}`;
    updatePublishButton();
  }

  function csrfToken() {
    const field = document.querySelector(
      "#cms-editor-csrf input[name=csrfmiddlewaretoken]",
    );
    if (field) return field.value;
    const row = document.cookie
      .split(";")
      .map((item) => item.trim())
      .find((item) => item.startsWith("csrftoken="));
    return row ? decodeURIComponent(row.split("=")[1]) : "";
  }

  async function request(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers: {"X-CSRFToken": csrfToken(), ...(options.headers || {})},
    });
    const isJson = (response.headers.get("Content-Type") || "").includes("application/json");
    const payload = isJson ? await response.json() : null;
    if (!response.ok) throw payload || {detail: `Request failed (HTTP ${response.status}).`};
    return payload;
  }

  function send(url, body) {
    return request(url, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
  }

  function uploadBlob(file) {
    const form = new FormData();
    form.append("file", file);
    return request(endpoints.blob_upload, {method: "POST", body: form});
  }

  function showError(error) {
    errorsRoot.hidden = false;
    errorsRoot.replaceChildren();
    errorsRoot.appendChild(element("strong", {}, errorText(error)));
    (error && Array.isArray(error.problems) ? error.problems : []).forEach((problem) => {
      errorsRoot.appendChild(element("div", {}, `${problem.path}: ${problem.message}`));
    });
  }

  function clearError() {
    errorsRoot.hidden = true;
    errorsRoot.replaceChildren();
  }

  saveButton.addEventListener("click", async () => {
    clearError();
    saveButton.disabled = true;
    statusRoot.textContent = "Saving…";
    try {
      if (state.is_new) {
        const result = await send(endpoints.create, {
          type: state.type,
          schema_version: schema.schema_version,
          data: state.data,
        });
        window.location.href = `${endpoints.changelist}${result.content_id}/editor/`;
        return;
      }
      const result = await send(endpoints.revision, {
        expected_version: state.content_version,
        schema_version: schema.schema_version,
        data: state.data,
      });
      state.content_version = result.content_version;
      state.revision_id = result.revision_id;
      state.revision_number = result.revision_number;
      statusRoot.textContent = "Saved.";
      render();
      loadReadiness();
    } catch (error) {
      statusRoot.textContent = "Save failed.";
      showError(error);
    } finally {
      saveButton.disabled = false;
    }
  });

  publishButton.addEventListener("click", async () => {
    clearError();
    publishing = true;
    updatePublishButton();
    statusRoot.textContent = "Publishing…";
    try {
      const result = await send(endpoints.publish, {
        expected_version: state.content_version,
        revision_id: state.revision_id,
      });
      state.content_version = result.content_version;
      state.published_revision_id = result.revision_id;
      statusRoot.textContent = "Published.";
      render();
    } catch (error) {
      statusRoot.textContent = "Publish failed.";
      showError(error);
      loadReadiness();
    } finally {
      publishing = false;
      updatePublishButton();
    }
  });

  render();
  renderReadiness();
  loadReadiness();
})();
