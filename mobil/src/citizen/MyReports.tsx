// Rapoartele trimise de pe acest telefon, cu starea lor live: verificat, pe ruta, colectat.
import { useEffect, useState } from 'react'
import { Camera } from 'lucide-react'

import { getAlerts, photoUrl, type Alert } from '../api'
import { loadReports, timeAgo } from '../lib'

const STEPS = ['Trimis', 'Verificat', 'Pe rută', 'Colectat']

function stepOf(a: Alert | undefined) {
  if (!a) return 1
  if (a.status === 'colectata') return 3
  if (a.status === 'asignata') return 2
  return 1
}

export default function MyReports({ onNew }: { onNew: () => void }) {
  const [reports] = useState(loadReports)
  const [alerts, setAlerts] = useState<Record<string, Alert>>({})

  useEffect(() => {
    if (!reports.length) return
    let alive = true
    const load = () => getAlerts()
      .then(list => alive && setAlerts(Object.fromEntries(list.map(a => [a.id, a]))))
      .catch(() => { /* starea ramane cea de la ultima incarcare */ })
    load()
    const id = setInterval(load, 15_000)
    return () => { alive = false; clearInterval(id) }
  }, [reports.length])

  const collected = reports.filter(r => alerts[r.id]?.status === 'colectata').length

  return (
    <div className="screen screen-pad">
      <header className="screen-head">
        <h1>Rapoartele mele</h1>
        <p>Urmărește ce se întâmplă cu fiecare raport trimis.</p>
      </header>

      {reports.length > 0 && (
        <div className="impact">
          <div><strong>{reports.length}</strong><span>trimise</span></div>
          <div><strong>{collected}</strong><span>colectate</span></div>
          <div><strong>{reports.length - collected}</strong><span>în lucru</span></div>
        </div>
      )}

      {!reports.length ? (
        <div className="empty">
          <span className="empty-icon"><Camera size={26} /></span>
          <strong>Niciun raport încă</strong>
          <p>Ai văzut deșeuri aruncate unde nu trebuie? O poză e suficientă ca să trimitem un camion.</p>
          <button className="btn btn-primary" onClick={onNew}>Fă primul raport</button>
        </div>
      ) : (
        <ul className="reports">
          {reports.map(r => {
            const a = alerts[r.id]
            const step = stepOf(a)
            return (
              <li key={r.id} className="card report">
                <img src={photoUrl(r.id)} alt="" loading="lazy" onError={e => { e.currentTarget.style.visibility = 'hidden' }} />
                <div className="report-body">
                  <div className="report-top">
                    <strong>{a?.status === 'colectata' ? 'Colectată' : a?.status === 'asignata' ? `Pe ruta camionului ${(a.camion ?? 0) + 1}` : 'Verificată'}</strong>
                    <time>{timeAgo(r.createdAt)}</time>
                  </div>
                  <p>{r.descriere}</p>
                  <ol className="steps" aria-label={`Pasul ${step + 1} din ${STEPS.length}`}>
                    {STEPS.map((s, i) => <li key={s} className={i <= step ? 'done' : ''}><span />{s}</li>)}
                  </ol>
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
