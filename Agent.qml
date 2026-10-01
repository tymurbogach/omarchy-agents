import QtQuick
import Quickshell.Io

// One agent's usage record, read straight off the data file that
// omarchy-agent-usage-update maintains. The panel never learns how the
// numbers were made — a record that appears in the usage directory is an
// agent, whoever wrote it.
Item {
  id: root
  visible: false

  property string agentId: ""
  property string path: ""
  property var record: null
  property bool reloadPending: false

  FileView {
    id: fileView
    path: root.path
    watchChanges: true
    printErrors: false
    onFileChanged: root.scheduleReload()
    onLoaded: root.parse(text())
    // Keep the last good record on transient read errors (ENOENT during an
    // atomic replace, momentary permissions): clearing to null would flash
    // the provider to zero and trigger a full recompute downstream.
    onLoadFailed: function(error) { console.warn("agents", "Usage record unreadable, keeping last good", root.path, error) }
  }

  function scheduleReload() {
    if (root.path === "") return
    if (reloadPending) return
    reloadPending = true
    Qt.callLater(doReload)
  }

  function doReload() {
    reloadPending = false
    if (root.path === "") return
    // FileView has no reload() guard for empty path; reassign triggers load.
    fileView.reload()
  }

  function parse(content) {
    var parsed = null
    try {
      parsed = JSON.parse(String(content || ""))
    } catch (e) {
      console.warn("agents", "Ignoring bad usage record", root.path, e)
      return
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return
    if (typeof parsed.id !== "string" || parsed.id === "") return
    if (root.agentId !== "" && parsed.id !== root.agentId) return
    // Record contract: schemaVersion 1, limits as a list when present.
    // A record that breaks the contract is rejected here so a corrupt or
    // future-version collector cannot render as stale v1 data downstream.
    if (parsed.schemaVersion !== undefined && parsed.schemaVersion !== 1) {
      console.warn("agents", "Ignoring usage record with unsupported schemaVersion", root.path, parsed.schemaVersion)
      return
    }
    if (parsed.limits !== undefined && parsed.limits !== null && !Array.isArray(parsed.limits)) {
      console.warn("agents", "Ignoring usage record with non-list limits", root.path)
      return
    }
    // Same content, same object: reassigning would emit recordChanged and
    // cascade into dataRevision++ plus a full provider recompute.
    try {
      if (JSON.stringify(parsed) === JSON.stringify(root.record)) return
    } catch (e) {}
    root.record = parsed
  }
}
