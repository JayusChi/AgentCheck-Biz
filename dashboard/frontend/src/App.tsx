import { NavLink, Route, Routes, useMatch } from "react-router-dom";
import { DefineAgent } from "./pages/DefineAgent";
import { BusinessRuns } from "./pages/BusinessRuns";

export default function App() {
  const isBusinessPage = useMatch("/business");
  return (
    <>
      <header className="app-header">
        <div className="logo-container">
          <h1>AgentCheck</h1>
          <span className="version-badge">演示版</span>
        </div>
        <nav className="biz-nav"><NavLink to="/" end>模型工作台</NavLink><NavLink to="/business">业务结果验证</NavLink></nav>
      </header>

      <main className={`app-main${isBusinessPage ? " app-main--business" : ""}`}>
        <Routes>
          <Route path="/business" element={<BusinessRuns />} />
          <Route path="/" element={<DefineAgent />} />
          <Route path="/define" element={<DefineAgent />} />
          <Route path="*" element={<DefineAgent />} />
        </Routes>
      </main>
    </>
  );
}
