(function () {
  "use strict";

  var bootstrapEl = document.getElementById("agent-team-bootstrap");
  if (!bootstrapEl) {
    return;
  }

  var bootstrap = JSON.parse(bootstrapEl.textContent || "{}");
  var logPaused = false;

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function setText(selector, value) {
    var node = document.querySelector(selector);
    if (node) {
      node.textContent = value === null || value === undefined || value === "" ? "none" : String(value);
    }
  }

  function setLiveStatus(message, stale) {
    var node = document.querySelector("[data-live-status]");
    if (!node) {
      return;
    }
    node.textContent = message;
    node.classList.toggle("stale", !!stale);
  }

  function fetchJson(path) {
    return fetch(path, { cache: "no-store", headers: { "Accept": "application/json" } }).then(function (response) {
      if (!response.ok) {
        throw new Error("HTTP " + response.status);
      }
      return response.json();
    });
  }

  function replaceWithEmpty(container, message) {
    container.replaceChildren(el("p", "muted", message));
  }

  var phaseLabels = {
    draft: "Draft",
    needs_research: "Needs research",
    researching: "Researching",
    ready_for_plan: "Ready for planning",
    planning: "Planning",
    awaiting_plan_approval: "Plan approval needed",
    ready_for_implementation: "Ready for implementation",
    implementing: "Implementing",
    ready_for_validation: "Ready for validation",
    validating: "Validating",
    ready_for_review: "Ready for review",
    reviewing: "Reviewing",
    awaiting_merge_approval: "Merge approval needed",
    ready_for_merge: "Ready to merge",
    merging: "Merging",
    ready_for_merge_conflict_resolution: "Ready to resolve merge conflicts",
    resolving_merge_conflicts: "Resolving merge conflicts",
    awaiting_human_input: "Human input needed",
    blocked: "Blocked",
    done: "Done"
  };

  function phaseLabel(phase) {
    var key = String(phase || "");
    return phaseLabels[key] || key.replace(/_/g, " ");
  }

  function issueHref(issueId) {
    var path = "/issues/" + issueId;
    if (bootstrap.repo) {
      path += "?repo=" + encodeURIComponent(bootstrap.repo);
    }
    return path;
  }

  function issueLine(item) {
    var li = el("li");
    var link = el("a");
    link.href = issueHref(item.id);
    link.textContent = "#" + item.id + " " + (item.title || "");
    var meta = el("span", "muted", "P" + item.priority + " - " + phaseLabel(item.phase) + " - updated " + item.updated_at);
    li.append(link, el("br"), meta);
    if (item.blocked_summary) {
      li.append(el("br"), el("span", "muted blocked-summary-inline", item.blocked_summary));
    }
    return li;
  }

  function renderIssues(container, items, emptyMessage) {
    if (!items || items.length === 0) {
      replaceWithEmpty(container, emptyMessage);
      return;
    }
    var ul = el("ul", "item-list");
    items.forEach(function (item) {
      ul.append(issueLine(item));
    });
    container.replaceChildren(ul);
  }

  function renderActive(container, items) {
    if (!items || items.length === 0) {
      replaceWithEmpty(container, "No active locks/runs.");
      return;
    }
    var ul = el("ul", "item-list");
    items.forEach(function (item) {
      var li = issueLine(item);
      li.append(el("br"), el("span", "muted", "owner " + (item.lock_owner || "unknown") + " - expires " + (item.lock_expires_at || "unknown")));
      ul.append(li);
    });
    container.replaceChildren(ul);
  }

  function renderRuns(container, items) {
    if (!items || items.length === 0) {
      replaceWithEmpty(container, "No runs yet.");
      return;
    }
    var ul = el("ul", "item-list");
    items.forEach(function (run) {
      var li = el("li");
      var issue = run.issue_id ? "issue #" + run.issue_id + " - " : "";
      li.append(
        el("strong", null, issue + phaseLabel(run.phase) + " - " + run.status),
        el("span", "muted", run.summary || "No summary recorded.")
      );
      ul.append(li);
    });
    container.replaceChildren(ul);
  }

  function renderRecentlyMerged(container, items) {
    if (!items || items.length === 0) {
      replaceWithEmpty(container, "No merged issues yet.");
      return;
    }
    var ul = el("ul", "item-list");
    items.forEach(function (merge) {
      var li = el("li");
      var link = el("a");
      link.href = issueHref(merge.issue_id);
      link.textContent = "#" + merge.issue_id + " " + (merge.title || "");
      var mergedAt = merge.completed_at || merge.started_at || merge.updated_at || "unknown time";
      var meta = "merged " + mergedAt + " - run " + String(merge.run_id || "").slice(0, 8);
      if (merge.summary) {
        meta += " - " + merge.summary;
      }
      li.append(link, el("br"), el("span", "muted", meta));
      ul.append(li);
    });
    container.replaceChildren(ul);
  }

  function renderEvents(container, items) {
    if (!items || items.length === 0) {
      replaceWithEmpty(container, "No events yet.");
      return;
    }
    var ul = el("ul", "item-list");
    items.forEach(function (event) {
      var li = el("li");
      var issue = event.issue_id ? "issue #" + event.issue_id + " - " : "";
      li.append(
        el("strong", null, event.event_type),
        el("span", "muted", issue + event.created_at + " - " + (event.message || ""))
      );
      ul.append(li);
    });
    container.replaceChildren(ul);
  }

  function renderJobs(container, items) {
    if (!items || items.length === 0) {
      replaceWithEmpty(container, "No queued browser actions in this server session.");
      return;
    }
    var ul = el("ul", "item-list");
    items.forEach(function (job) {
      var li = el("li");
      li.append(
        el("strong", null, job.status + " - " + job.action),
        el("span", "muted", String(job.id).slice(0, 8) + " - " + (job.message || ""))
      );
      ul.append(li);
    });
    container.replaceChildren(ul);
  }

  function renderPhaseCounts(container, items) {
    if (!items || items.length === 0) {
      replaceWithEmpty(container, "No issues.");
      return;
    }
    var ul = el("ul", "item-list");
    items.forEach(function (item) {
      ul.append(el("li", null, item.status + " - " + item.phase + ": " + item.count));
    });
    container.replaceChildren(ul);
  }

  function renderTimeline(container, steps) {
    if (!container || !steps) {
      return;
    }
    var nodes = steps.map(function (step) {
      var artifact = step.artifact;
      var className = "phase-step " + step.status + (artifact && artifact.url ? " has-artifact" : "");
      if (artifact && artifact.url) {
        var link = el("a", className, step.label);
        var artifactLabel = artifact.label || (String(step.label || "").toLowerCase() + " artifact");
        var title = "Open " + artifactLabel;
        link.href = artifact.url;
        link.title = title;
        link.setAttribute("aria-label", title);
        return link;
      }
      return el("span", className, step.label);
    });
    container.replaceChildren.apply(container, nodes);
  }

  function renderArtifacts(container, items) {
    if (!items || items.length === 0) {
      replaceWithEmpty(container, "No artifacts or logs yet.");
      return;
    }
    var ul = el("ul", "item-list");
    items.forEach(function (artifact) {
      var li = el("li");
      var link = el("a");
      link.href = artifact.url;
      link.textContent = artifact.label;
      li.append(
        el("strong", null, artifact.kind),
        link,
        el("br"),
        el("span", "muted", artifact.relative_path + " - " + artifact.size_bytes + " bytes - " + artifact.modified_at)
      );
      ul.append(li);
    });
    container.replaceChildren(ul);
  }

  function blockedReasonSourceLabel(source) {
    if (source === "run") {
      return "agent run";
    }
    if (source === "manual_transition") {
      return "manual transition";
    }
    if (source === "transition") {
      return "transition";
    }
    return "recorded state";
  }

  function renderBlockedReasonLink(container, link) {
    if (!link || !link.url) {
      return;
    }
    var anchor = el("a", "button", link.label || "Open details");
    anchor.href = link.url;
    container.append(anchor, document.createTextNode(" "));
  }

  function renderBlockedReason(container, reason) {
    if (!container) {
      return;
    }
    if (!reason) {
      container.replaceChildren();
      container.hidden = true;
      container.className = "";
      return;
    }

    container.hidden = false;
    container.className = "panel attention blocked-reason-panel";
    var summary = reason.summary || reason.headline || "No blocked reason was recorded.";

    var nodes = [
      el("h2", null, "Blocked reason"),
      el("p", "blocked-reason-summary", summary)
    ];
    if (reason.suggested_transition && reason.suggested_transition.label) {
      nodes.push(el("p", "muted", "Suggested retry: " + reason.suggested_transition.label));
    }
    nodes.push(blockedReasonTechnicalDetails(reason, summary));
    container.replaceChildren.apply(container, nodes);
  }

  function blockedReasonTechnicalDetails(reason, summary) {
    var metaParts = ["Source: " + blockedReasonSourceLabel(reason.source)];
    if (reason.phase) metaParts.push("Phase: " + reason.phase);
    if (reason.status) metaParts.push("Status: " + reason.status);
    if (reason.run_id) metaParts.push("Run: " + String(reason.run_id).slice(0, 8));
    if (reason.started_at) metaParts.push("Started: " + reason.started_at);
    if (reason.completed_at) metaParts.push("Completed: " + reason.completed_at);
    if (reason.suggested_transition && reason.suggested_transition.label) {
      metaParts.push("Suggested retry: " + reason.suggested_transition.label);
    }

    var details = el("details", "blocked-reason-technical");
    details.append(el("summary", null, "Technical details"));
    details.append(el("p", "muted", metaParts.join(" - ")));
    if (reason.technical_summary && reason.technical_summary !== summary) {
      details.append(el("p", "blocked-reason-technical-summary", reason.technical_summary));
    }
    if (reason.run_summary && reason.run_summary !== summary && reason.run_summary !== reason.technical_summary) {
      details.append(el("p", "blocked-reason-run-summary", reason.run_summary));
    }
    if (reason.error) {
      details.append(el("pre", "blocked-reason-error", reason.error));
    }
    if (
      reason.artifact_excerpt &&
      reason.artifact_excerpt !== summary &&
      reason.artifact_excerpt !== reason.technical_summary &&
      reason.artifact_excerpt !== reason.run_summary
    ) {
      details.append(el("p", "blocked-reason-artifact-excerpt", reason.artifact_excerpt));
    }
    var links = el("p", "blocked-reason-links");
    renderBlockedReasonLink(links, reason.artifact);
    renderBlockedReasonLink(links, reason.log);
    if (links.children.length > 0) {
      details.append(links);
    }
    return details;
  }

  function renderClosedSynopsisLink(list, link) {
    if (!link || !link.url) {
      return;
    }
    var item = el("li");
    var anchor = el("a");
    anchor.href = link.url;
    anchor.textContent = link.label || link.relative_path || "Open details";
    item.append(anchor);
    list.append(item);
  }

  function renderClosedSynopsis(container, synopsis) {
    if (!container) {
      return;
    }
    if (!synopsis) {
      container.replaceChildren();
      container.hidden = true;
      container.className = "";
      return;
    }

    container.hidden = false;
    container.className = "panel closed-synopsis-panel";
    var details = ["Source: " + (synopsis.source || "recorded state")];
    if (synopsis.completed_at) details.push("Completed: " + synopsis.completed_at);
    if (synopsis.merged_at) details.push("Merged: " + synopsis.merged_at);
    if (synopsis.target_branch) details.push("Target branch: " + synopsis.target_branch);
    if (synopsis.merge_commit) details.push("Merge commit: " + synopsis.merge_commit);
    if (synopsis.worktree_commit) details.push("Worktree commit: " + synopsis.worktree_commit);
    var nodes = [
      el("h2", null, "Closed synopsis"),
      el("p", "closed-synopsis-summary", synopsis.summary || synopsis.headline || "No detailed closed synopsis is available."),
      el("p", "muted", details.join(" - "))
    ];
    if (synopsis.change_excerpt) {
      nodes.push(el("p", "closed-synopsis-change", synopsis.change_excerpt));
    }
    if (synopsis.merge_summary) {
      nodes.push(el("p", "closed-synopsis-merge", synopsis.merge_summary));
    }
    var links = el("ul", "item-list closed-synopsis-links");
    (synopsis.links || []).forEach(function (link) {
      renderClosedSynopsisLink(links, link);
    });
    if (links.children.length > 0) {
      nodes.push(links);
    }
    container.replaceChildren.apply(container, nodes);
  }

  function renderField(field) {
    var control;
    if (field.type === "textarea") {
      control = el("textarea");
      control.rows = field.rows || 3;
    } else if (field.type === "select") {
      control = el("select");
      (field.options || []).forEach(function (optionData) {
        var option = el("option", null, optionData.label || optionData.value);
        option.value = optionData.value;
        if (field.value !== undefined && String(field.value) === String(optionData.value)) {
          option.selected = true;
        }
        control.append(option);
      });
    } else {
      control = el("input");
      control.type = field.type === "input" ? "text" : (field.type || "text");
    }
    control.name = field.name || "";
    if (field.value !== undefined && field.value !== null && field.type !== "select") {
      control.value = String(field.value);
    }
    if (field.placeholder) {
      control.placeholder = field.placeholder;
    }
    if (field.required) {
      control.required = true;
    }
    if (!field.label) {
      return control;
    }
    var label = el("label");
    label.append(document.createTextNode(field.label + " "), control);
    return label;
  }

  function controlsSignature(controls) {
    return JSON.stringify(controls || []);
  }

  function runtimeText(runtime) {
    if (!runtime) {
      return "Mode: web-only";
    }
    if (runtime.mode === "serve") {
      return "Mode: serve - autonomous workers " + runtime.worker_concurrency
        + " - poll interval " + runtime.worker_interval_seconds + "s"
        + " - queued browser actions " + runtime.web_workers;
    }
    return "Mode: web-only - queued browser actions " + runtime.web_workers;
  }

  function updateControlCsrf(container, csrfToken) {
    Array.prototype.forEach.call(container.querySelectorAll('input[name="_csrf_token"]'), function (field) {
      field.value = csrfToken || "";
    });
  }

  function renderControls(container, controls, csrfToken, signature) {
    if (!container) {
      return;
    }
    var nextSignature = signature || controlsSignature(controls);
    if (container.getAttribute("data-controls-signature") === nextSignature) {
      updateControlCsrf(container, csrfToken);
      return;
    }
    if (!controls || controls.length === 0) {
      replaceWithEmpty(container, "No actions are available for this phase.");
      container.setAttribute("data-controls-signature", nextSignature);
      return;
    }
    function controlGroup(control) {
      if (control.group === "primary" || control.group === "advanced" || control.group === "danger") {
        return control.group;
      }
      var action = String(control.action || control.href || "");
      if (action.endsWith("/actions/transition")) return "advanced";
      if (action.endsWith("/actions/reset-to-draft") || action.endsWith("/actions/delete")) return "danger";
      return "primary";
    }
    function renderControl(control) {
      if (control.kind === "link") {
        var anchor = el("a", "button", control.button || "Open");
        anchor.href = control.href || control.action || "";
        if (control.class_name) {
          anchor.className = "button " + control.class_name;
        }
        return anchor;
      }
      var form = el("form");
      form.setAttribute("method", control.method || "post");
      form.setAttribute("action", control.action || "");
      if (control.class_name) {
        form.className = control.class_name;
      }
      var fieldset = el("fieldset");
      fieldset.append(el("legend", null, control.button || "Submit"));
      var csrf = el("input");
      csrf.type = "hidden";
      csrf.name = "_csrf_token";
      csrf.value = csrfToken || "";
      fieldset.append(csrf);
      (control.fields || []).forEach(function (field) {
        fieldset.append(renderField(field));
      });
      var button = el("button", null, control.button || "Submit");
      button.type = "submit";
      fieldset.append(button);
      form.append(fieldset);
      return form;
    }
    function appendControls(parent, items) {
      items.forEach(function (control) {
        parent.append(renderControl(control));
      });
    }
    var primary = controls.filter(function (control) { return controlGroup(control) === "primary"; });
    var advanced = controls.filter(function (control) { return controlGroup(control) === "advanced"; });
    var danger = controls.filter(function (control) { return controlGroup(control) === "danger"; });
    var nodes = [];
    if (primary.length > 0) {
      var primaryGroup = el("div", "control-group primary-action-group");
      appendControls(primaryGroup, primary);
      nodes.push(primaryGroup);
    } else {
      nodes.push(el("p", "muted", "No primary action is available for this phase."));
    }
    if (advanced.length > 0) {
      var advancedGroup = el("details", "advanced-actions");
      advancedGroup.append(el("summary", null, "Advanced actions: override phase"));
      advancedGroup.append(el("p", "muted", "Use only when you intentionally need to move this issue to another machine phase."));
      appendControls(advancedGroup, advanced);
      nodes.push(advancedGroup);
    }
    if (danger.length > 0) {
      var dangerGroup = el("details", "danger-zone");
      dangerGroup.append(el("summary", null, "Danger zone: reset or delete this issue"));
      dangerGroup.append(el("p", "muted", "These actions are destructive and require exact confirmation text."));
      appendControls(dangerGroup, danger);
      nodes.push(dangerGroup);
    }
    container.replaceChildren.apply(container, nodes);
    container.setAttribute("data-controls-signature", nextSignature);
  }

  function updateDashboard(data) {
    Object.keys(data.summary || {}).forEach(function (key) {
      var card = document.querySelector('[data-summary-card="' + key + '"] [data-summary-count]');
      if (card) {
        card.textContent = data.summary[key];
      }
    });
    var active = document.querySelector('[data-dashboard-list="active_work"]');
    if (active) renderActive(active, data.active_work);
    var approvals = document.querySelector('[data-dashboard-list="approval_issues"]');
    if (approvals) renderIssues(approvals, data.approval_issues, "No approval gates waiting.");
    var humanInput = document.querySelector('[data-dashboard-list="human_input_needed"]');
    if (humanInput) renderIssues(humanInput, data.human_input_needed, "No human input requests waiting.");
    var drafts = document.querySelector('[data-dashboard-list="draft_issues"]');
    if (drafts) renderIssues(drafts, data.draft_issues, "No draft issues.");
    var blocked = document.querySelector('[data-dashboard-list="blocked_issues"]');
    if (blocked) renderIssues(blocked, data.blocked_issues, "No blocked issues.");
    var ready = document.querySelector('[data-dashboard-list="ready_issues"]');
    if (ready) renderIssues(ready, data.ready_issues, "No ready issues.");
    var open = document.querySelector('[data-dashboard-list="open_issues"]');
    if (open) renderIssues(open, data.open_issues, "No open issues.");
    var recentlyMerged = document.querySelector('[data-dashboard-list="recently_merged"]');
    if (recentlyMerged) renderRecentlyMerged(recentlyMerged, data.recently_merged);
    var runs = document.querySelector('[data-dashboard-list="recent_runs"]');
    if (runs) renderRuns(runs, data.recent_runs);
    var events = document.querySelector('[data-dashboard-list="recent_events"]');
    if (events) renderEvents(events, data.recent_events);
    var counts = document.querySelector('[data-dashboard-list="phase_counts"]');
    if (counts) renderPhaseCounts(counts, data.phase_counts);
    var jobs = document.querySelector('[data-dashboard-list="jobs"]');
    if (jobs) renderJobs(jobs, data.jobs);
    setText("[data-runtime-status]", runtimeText(data.runtime));
    setLiveStatus("Updated " + data.generated_at, false);
  }

  function jobText(job) {
    return job ? job.status + " - " + job.message : "none";
  }

  function updateIssue(data) {
    setText("[data-issue-title]", data.issue.title);
    setText("[data-issue-phase]", data.issue.phase);
    setText("[data-issue-phase-label]", phaseLabel(data.issue.phase));
    setText("[data-issue-status]", data.issue.status);
    setText("[data-issue-priority]", "P" + data.issue.priority);
    setText("[data-issue-repo]", data.issue.repo_path);
    setText("[data-issue-tags]", data.issue.tags);
    setText("[data-issue-description]", data.issue.description);
    setText("[data-current-run]", data.issue.current_run_id);
    setText("[data-lock-owner]", data.issue.lock_owner);
    setText("[data-lock-expiry]", data.issue.lock_expires_at);
    setText("[data-active-job]", jobText(data.active_job));
    setText("[data-next-action]", data.next_action);
    renderBlockedReason(document.querySelector("[data-blocked-reason]"), data.blocked_reason);
    renderClosedSynopsis(document.querySelector("[data-closed-synopsis]"), data.closed_synopsis);
    renderTimeline(document.querySelector("[data-phase-timeline]"), data.phase_timeline);
    var events = document.querySelector("[data-issue-events]");
    if (events) renderEvents(events, data.recent_events);
    var runs = document.querySelector("[data-issue-runs]");
    if (runs) renderRuns(runs, data.recent_runs);
    var artifacts = document.querySelector("[data-issue-artifacts]");
    if (artifacts) renderArtifacts(artifacts, data.artifacts);
    renderControls(
      document.querySelector("[data-action-stack]"),
      data.manager_controls,
      data.csrf_token || bootstrap.csrf_token,
      data.manager_controls_signature
    );
    setLiveStatus("Updated " + data.generated_at, false);
  }

  function updateLog(data) {
    var log = data.log || {};
    var meta = document.querySelector("[data-log-meta]");
    var output = document.querySelector("[data-log-output]");
    if (meta) {
      var size = log.size_bytes || 0;
      var trunc = log.truncated ? "tail, " : "";
      meta.textContent = log.relative_path ? log.relative_path + " (" + trunc + size + " bytes)" : "No run log yet";
    }
    if (!output || logPaused) {
      return;
    }
    var wasAtBottom = output.scrollTop + output.clientHeight >= output.scrollHeight - 8;
    output.textContent = log.content || "";
    if (wasAtBottom) {
      output.scrollTop = output.scrollHeight;
    }
  }

  function startLoop(work, intervalMs) {
    function tick() {
      var delay = document.hidden ? Math.max(intervalMs * 4, 10000) : intervalMs;
      work().catch(function (error) {
        setLiveStatus("Live update failed: " + error.message, true);
      }).finally(function () {
        window.setTimeout(tick, delay);
      });
    }
    tick();
  }

  if (bootstrap.page === "dashboard") {
    startLoop(function () {
      return fetchJson(bootstrap.dashboard_api_url || "/api/dashboard").then(updateDashboard);
    }, 2500);
  }

  if (bootstrap.page === "issue") {
    var issueId = bootstrap.issue_id;
    var toggle = document.querySelector("[data-log-toggle]");
    if (toggle) {
      toggle.addEventListener("click", function () {
        logPaused = !logPaused;
        toggle.textContent = logPaused ? "Resume log" : "Pause log";
        toggle.setAttribute("aria-pressed", logPaused ? "true" : "false");
      });
    }
    startLoop(function () {
      return fetchJson(bootstrap.issue_api_url || ("/api/issues/" + issueId)).then(updateIssue);
    }, 2500);
    startLoop(function () {
      if (logPaused) {
        return Promise.resolve();
      }
      return fetchJson(bootstrap.log_api_url || ("/api/issues/" + issueId + "/logs/current")).then(updateLog);
    }, 2000);
  }
}());