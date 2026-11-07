import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { Navigate, NavLink, Route, Routes } from "react-router";
import { api, unwrap, type Role } from "./api/client";
import { ROLE_LABELS, useIdentity } from "./identity";
import { useApi } from "./queries";
import { FindingDetail } from "./views/FindingDetail";
import { FleetView } from "./views/Fleet";
import { LedgerView } from "./views/Ledger";
import { PortfolioView } from "./views/Portfolio";
import { RbqmView } from "./views/Rbqm";
import { ReviewQueue } from "./views/ReviewQueue";
import { StudyView } from "./views/Study";
import { UnblindedView } from "./views/Unblinded";

/** Where each role lands: decision-making roles on the review queue. */
export function homeFor(role: Role): string {
  switch (role) {
    case "portfolio_lead":
    case "toxicologist":
      return "/portfolio";
    case "unblinded_statistician":
      return "/unblinded";
    default:
      return "/review";
  }
}

function TopBar({
  menuOpen,
  onMenu,
  menuButton,
}: {
  menuOpen: boolean;
  onMenu: () => void;
  menuButton: RefObject<HTMLButtonElement | null>;
}) {
  const { people, person, choose } = useIdentity();
  const unblinded = person.role === "unblinded_statistician";
  return (
    <header className="topbar">
      <div className="topbar-row">
        <button
          ref={menuButton}
          type="button"
          className="btn menu-button"
          aria-expanded={menuOpen}
          aria-controls="nav-panel"
          onClick={onMenu}
        >
          Menu
        </button>
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            M
          </span>
          <span>
            <span className="brand-name">Meridian Command Center</span>
            <br />
            <span className="brand-sub">Clinical development agent fleet</span>
          </span>
        </div>
        <div className="switcher">
          <label htmlFor="person">Acting as</label>
          <select
            id="person"
            value={person.actor}
            onChange={(e) => {
              choose(e.target.value);
            }}
          >
            {people.map((p) => (
              <option key={p.actor} value={p.actor}>
                {p.name}, {ROLE_LABELS[p.role]}
              </option>
            ))}
          </select>
          <span className={`pill ${unblinded ? "unblinded" : "blinded"}`}>
            {unblinded ? "Unblinded view" : "Blinded view"}
          </span>
        </div>
      </div>
      <p className="dev-banner" role="note">
        Development identity: not real sign-in. Anyone can pick any person here.
      </p>
    </header>
  );
}

interface NavItem {
  to?: string;
  label: string;
  restricted?: boolean;
}

function Nav({ onNavigate }: { onNavigate?: () => void }) {
  const { person } = useIdentity();
  const queue = useApi(["review-queue", "count"], () => unwrap(api.GET("/api/v1/review-queue")));
  const unblinded = person.role === "unblinded_statistician";
  const groups: [string, NavItem[]][] = [
    [
      "Development",
      [
        { to: "/portfolio", label: "Portfolio" },
        { label: "Preclinical" },
        { to: "/study", label: "Study MRD-201" },
      ],
    ],
    [
      "Oversight",
      [{ to: "/rbqm", label: "RBQM" }, { label: "Safety" }, { label: "Quality and audit" }],
    ],
    ["Decisions", [{ to: "/review", label: "Review queue" }]],
    [
      "Platform",
      [
        { to: "/fleet", label: "Agent fleet" },
        { to: "/ledger", label: "Event ledger" },
      ],
    ],
    [
      "Restricted",
      [unblinded ? { to: "/unblinded", label: "Unblinded zone", restricted: true } : { label: "Unblinded zone" }],
    ],
  ];
  return (
    <nav className="nav" aria-label="Views">
      {groups.map(([group, items]) => (
        <div key={group} role="group" aria-label={group}>
          <div className="group label">{group}</div>
          {items.map((item) =>
            item.to ? (
              <NavLink
                key={item.label}
                to={item.to}
                className={item.restricted ? "restricted" : undefined}
                onClick={onNavigate}
              >
                {item.label}
                {item.to === "/review" && queue.data ? (
                  <span className="count" aria-label={`${queue.data.items.length} open`}>
                    {queue.data.items.length}
                  </span>
                ) : null}
              </NavLink>
            ) : (
              <span key={item.label} className="disabled" aria-disabled="true">
                {item.label}
                <small>arrives in a later step</small>
              </span>
            ),
          )}
        </div>
      ))}
    </nav>
  );
}

/** The sidebar as a panel on narrow screens. Escape and the close button dismiss it, and
 * focus returns to the Menu button. */
function NavPanel({ onClose }: { onClose: () => void }) {
  const closeButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeButton.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);
  return (
    <div className="nav-overlay">
      <div className="nav-backdrop" aria-hidden="true" onClick={onClose} />
      <div id="nav-panel" className="nav-panel" role="dialog" aria-modal="true" aria-label="Menu">
        <div className="nav-panel-head">
          <span className="label">Menu</span>
          <button ref={closeButton} type="button" className="btn" onClick={onClose}>
            Close
          </button>
        </div>
        <Nav onNavigate={onClose} />
      </div>
    </div>
  );
}

export function App() {
  const { person } = useIdentity();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const closeMenu = useCallback(() => {
    setMenuOpen(false);
    menuButton.current?.focus();
  }, []);
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <TopBar
        menuOpen={menuOpen}
        menuButton={menuButton}
        onMenu={() => {
          setMenuOpen((open) => !open);
        }}
      />
      {menuOpen ? <NavPanel onClose={closeMenu} /> : null}
      <div className="shell">
        <Nav />
        <main id="main" tabIndex={-1}>
          <Routes>
            <Route path="/" element={<Navigate to={homeFor(person.role)} replace />} />
            <Route path="/review" element={<ReviewQueue />} />
            <Route path="/findings/:findingId" element={<FindingDetail />} />
            <Route path="/rbqm" element={<RbqmView />} />
            <Route path="/study" element={<StudyView />} />
            <Route path="/ledger" element={<LedgerView />} />
            <Route path="/fleet" element={<FleetView />} />
            <Route path="/portfolio" element={<PortfolioView />} />
            <Route path="/unblinded" element={<UnblindedView />} />
            <Route path="*" element={<p className="message">This page does not exist.</p>} />
          </Routes>
        </main>
      </div>
    </>
  );
}
