import React, { useState, useEffect } from 'react'

const API_BASE = '/api'

export default function App() {
  const [drafts, setDrafts] = useState([])
  const [summary, setSummary] = useState({ total: 0, by_status: {} })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [editingDraft, setEditingDraft] = useState(null)
  const [editSubject, setEditSubject] = useState('')
  const [editBody, setEditBody] = useState('')
  const [filterPending, setFilterPending] = useState(true)
  const [actionMsg, setActionMsg] = useState(null)

  const fetchData = async () => {
    setLoading(true)
    setError(null)
    try {
      const url = filterPending ? `${API_BASE}/drafts?pending_only=true` : `${API_BASE}/drafts`
      const [draftsRes, statusRes] = await Promise.all([
        fetch(url),
        fetch(`${API_BASE}/status`)
      ])
      if (!draftsRes.ok) throw new Error(`Drafts error: ${draftsRes.statusText}`)
      if (!statusRes.ok) throw new Error(`Status error: ${statusRes.statusText}`)
      const draftsData = await draftsRes.json()
      const statusData = await statusRes.json()
      setDrafts(draftsData)
      setSummary(statusData)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
  }, [filterPending])

  const showNotification = (msg) => {
    setActionMsg(msg)
    setTimeout(() => setActionMsg(null), 4000)
  }

  const handleApprove = async (draftId) => {
    try {
      const res = await fetch(`${API_BASE}/drafts/${draftId}/approve`, { method: 'POST' })
      if (!res.ok) {
        const errData = await res.json()
        throw new Error(errData.detail || 'Approval failed')
      }
      showNotification(`Draft #${draftId} approved successfully!`)
      fetchData()
    } catch (err) {
      alert(`Error approving draft: ${err.message}`)
    }
  }

  const handleReject = async (draftId) => {
    if (!confirm(`Are you sure you want to reject draft #${draftId}? Professor will be marked as NEEDS_HUMAN.`)) {
      return
    }
    try {
      const res = await fetch(`${API_BASE}/drafts/${draftId}/reject`, { method: 'POST' })
      if (!res.ok) throw new Error('Reject failed')
      showNotification(`Draft #${draftId} rejected. Marked as NEEDS_HUMAN.`)
      fetchData()
    } catch (err) {
      alert(`Error rejecting draft: ${err.message}`)
    }
  }

  const startEdit = (draft) => {
    setEditingDraft(draft)
    setEditSubject(draft.subject)
    setEditBody(draft.body)
  }

  const handleSaveAndApprove = async (andApprove = true) => {
    if (!editingDraft) return
    try {
      const putRes = await fetch(`${API_BASE}/drafts/${editingDraft.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject: editSubject, body: editBody }),
      })
      if (!putRes.ok) throw new Error('Update failed')

      if (andApprove) {
        const appRes = await fetch(`${API_BASE}/drafts/${editingDraft.id}/approve`, { method: 'POST' })
        if (!appRes.ok) throw new Error('Approve after edit failed')
        showNotification(`Draft #${editingDraft.id} updated and approved!`)
      } else {
        showNotification(`Draft #${editingDraft.id} updated.`)
      }

      setEditingDraft(null)
      fetchData()
    } catch (err) {
      alert(`Error: ${err.message}`)
    }
  }

  const handleSend = async (profId, profName) => {
    if (!confirm(`Send email to ${profName}? (Dry run writes to outbox/)`)) return
    try {
      const res = await fetch(`${API_BASE}/send/${profId}`, { method: 'POST' })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Send failed')
      showNotification(`Email for ${profName} sent!`)
      fetchData()
    } catch (err) {
      alert(`Send error: ${err.message}`)
    }
  }

  return (
    <div className="container">
      <header className="header">
        <h1>Outreach Agent - Human Approval Gate</h1>
        <p className="subtitle">Review, edit, and approve outreach emails before sending.</p>
      </header>

      {/* Status Summary Bar */}
      <div className="summary-bar">
        <div className="stat-card">
          <span className="stat-label">Total Professors</span>
          <span className="stat-val">{summary.total}</span>
        </div>
        {Object.entries(summary.by_status || {}).map(([st, count]) => (
          <div key={st} className={`stat-card stat-${st.toLowerCase()}`}>
            <span className="stat-label">{st}</span>
            <span className="stat-val">{count}</span>
          </div>
        ))}
      </div>

      {actionMsg && <div className="alert-banner">{actionMsg}</div>}

      {/* Filter / Controls */}
      <div className="controls">
        <label className="toggle-label">
          <input
            type="checkbox"
            checked={filterPending}
            onChange={(e) => setFilterPending(e.target.checked)}
          />
          Show Pending Review Only
        </label>
        <button className="btn btn-secondary" onClick={fetchData}>Refresh</button>
      </div>

      {loading && <p className="loading">Loading drafts...</p>}
      {error && <div className="error-banner">Error: {error}</div>}

      {!loading && drafts.length === 0 && (
        <div className="empty-state">
          <h3>No drafts found</h3>
          <p>{filterPending ? 'All drafts have been reviewed or none are generated yet.' : 'No drafts in database.'}</p>
        </div>
      )}

      {/* Drafts List */}
      <div className="drafts-grid">
        {drafts.map((d) => {
          const wordCount = d.body.trim().split(/\s+/).filter(Boolean).length
          const isPending = !d.is_approved
          return (
            <div key={d.id} className={`draft-card ${isPending ? 'pending' : 'approved'}`}>
              <div className="draft-header">
                <div>
                  <span className="draft-id">#{d.id}</span>
                  <span className="draft-target">{d.professor_name || `Prof ID: ${d.professor_id}`}</span>
                  <span className="draft-uni">({d.university || d.professor_email})</span>
                </div>
                <div className="draft-badges">
                  <span className={`badge ${d.is_approved ? 'badge-approved' : 'badge-pending'}`}>
                    {d.is_approved ? 'APPROVED' : 'PENDING APPROVAL'}
                  </span>
                  {d.judge_score !== null && (
                    <span className="badge badge-judge">Judge: {d.judge_score.toFixed(2)}</span>
                  )}
                  <span className={`badge ${wordCount > 170 ? 'badge-warn' : 'badge-words'}`}>
                    {wordCount} words
                  </span>
                </div>
              </div>

              <div className="draft-meta">
                <div><strong>Email:</strong> {d.professor_email}</div>
                <div><strong>Kind:</strong> {d.kind} (seq {d.sequence})</div>
                <div><strong>Hash:</strong> <code>{d.text_hash.slice(0, 16)}...</code></div>
              </div>

              <div className="draft-subject">
                <strong>Subject:</strong> {d.subject}
              </div>

              <div className="draft-body">
                {d.body}
              </div>

              {d.referenced_claim && (
                <div className="draft-grounding">
                  <div className="grounding-claim">
                    <strong>Referenced claim:</strong> {d.referenced_claim}
                  </div>
                  {d.claim_source_quote && (
                    <div className="grounding-quote">
                      <strong>Paper quote:</strong> <em>"{d.claim_source_quote}"</em>
                    </div>
                  )}
                </div>
              )}

              <div className="draft-actions">
                {isPending ? (
                  <>
                    <button className="btn btn-approve" onClick={() => handleApprove(d.id)}>
                      Approve
                    </button>
                    <button className="btn btn-edit" onClick={() => startEdit(d)}>
                      Edit & Approve
                    </button>
                    <button className="btn btn-reject" onClick={() => handleReject(d.id)}>
                      Reject
                    </button>
                  </>
                ) : (
                  <>
                    <button className="btn btn-send" onClick={() => handleSend(d.professor_id, d.professor_name)}>
                      Send Now
                    </button>
                    <button className="btn btn-secondary" onClick={() => startEdit(d)}>
                      Edit (Re-opens)
                    </button>
                  </>
                )}
              </div>
            </div>
          )
        })}
      </div>

      {/* Edit Modal */}
      {editingDraft && (
        <div className="modal-backdrop">
          <div className="modal">
            <h2>Edit Draft #{editingDraft.id}</h2>
            <p className="modal-subtitle">
              Recipient: {editingDraft.professor_name} ({editingDraft.professor_email})
            </p>

            <div className="form-group">
              <label>Subject</label>
              <input
                type="text"
                value={editSubject}
                onChange={(e) => setEditSubject(e.target.value)}
              />
            </div>

            <div className="form-group">
              <label>Body ({editBody.trim().split(/\s+/).filter(Boolean).length} words, max 170)</label>
              <textarea
                rows={8}
                value={editBody}
                onChange={(e) => setEditBody(e.target.value)}
              />
            </div>

            <div className="modal-actions">
              <button className="btn btn-approve" onClick={() => handleSaveAndApprove(true)}>
                Save & Approve
              </button>
              <button className="btn btn-secondary" onClick={() => handleSaveAndApprove(false)}>
                Save as Draft
              </button>
              <button className="btn btn-reject" onClick={() => setEditingDraft(null)}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
