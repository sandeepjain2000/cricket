import React, { useState, useMemo } from 'react';
import { 
  Search, 
  BarChart2, 
  Users, 
  Activity, 
  Database, 
  Filter, 
  ChevronDown, 
  TrendingUp, 
  Target,
  Shield,
  Zap,
  MoreVertical,
  Plus,
  Trash2,
  Info,
  ArrowUpDown,
  MessageSquare,
  Sparkles,
  ChevronRight,
  ArrowUp,
  ArrowDown
} from 'lucide-react';

// Mock Data for Players
const INITIAL_PLAYERS = [
  { id: 1, name: "Virat Kohli", role: "Batter", avg: 52.7, sr: 138.4, clutch: 92, consistency: 95, matches: 280, country: "IND", image: "VK" },
  { id: 2, name: "Jos Buttler", role: "WK-Batter", avg: 41.2, sr: 144.1, clutch: 88, consistency: 82, matches: 165, country: "ENG", image: "JB" },
  { id: 3, name: "Rashid Khan", role: "Bowler", avg: 18.2, sr: 122.0, clutch: 96, consistency: 98, matches: 210, country: "AFG", image: "RK" },
  { id: 4, name: "Hardik Pandya", role: "All-Rounder", avg: 33.5, sr: 142.8, clutch: 94, consistency: 78, matches: 120, country: "IND", image: "HP" },
  { id: 5, name: "Jasprit Bumrah", role: "Bowler", avg: 12.1, sr: 110.0, clutch: 98, consistency: 97, matches: 145, country: "IND", image: "JBu" },
  { id: 6, name: "Suryakumar Yadav", role: "Batter", avg: 45.3, sr: 172.5, clutch: 85, consistency: 80, matches: 60, country: "IND", image: "SKY" },
  { id: 7, name: "Glenn Maxwell", role: "All-Rounder", avg: 29.8, sr: 155.2, clutch: 89, consistency: 65, matches: 138, country: "AUS", image: "GM" },
  { id: 8, name: "Babar Azam", role: "Batter", avg: 41.5, sr: 128.4, clutch: 82, consistency: 90, matches: 115, country: "PAK", image: "BA" },
  { id: 9, name: "Trent Boult", role: "Bowler", avg: 14.5, sr: 105.0, clutch: 87, consistency: 91, matches: 104, country: "NZ", image: "TB" },
  { id: 10, name: "Quinton de Kock", role: "WK-Batter", avg: 32.5, sr: 137.2, clutch: 80, consistency: 75, matches: 85, country: "SA", image: "QDK" },
  { id: 11, name: "Ben Stokes", role: "All-Rounder", avg: 35.6, sr: 136.8, clutch: 97, consistency: 72, matches: 140, country: "ENG", image: "BS" },
  { id: 12, name: "Kagiso Rabada", role: "Bowler", avg: 16.8, sr: 115.5, clutch: 85, consistency: 88, matches: 130, country: "SA", image: "KR" },
];

const ROLES = ['All', 'Batter', 'Bowler', 'All-Rounder', 'WK-Batter'];

const SidebarItem = ({ icon: Icon, label, active, onClick }) => (
  <button 
    onClick={onClick}
    className={`w-full flex items-center space-x-3 px-4 py-3 rounded-lg transition-all ${
      active 
        ? 'bg-emerald-600 text-white shadow-md shadow-emerald-200' 
        : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
    }`}
  >
    <Icon size={20} />
    <span className="font-medium text-sm">{label}</span>
  </button>
);

const App = () => {
  const [activeTab, setActiveTab] = useState('vault'); // Defaulted to vault as requested
  const [selectedPlayers, setSelectedPlayers] = useState([]);
  const [weights, setWeights] = useState({ clutch: 50, sr: 50, consistency: 50 });
  
  // Vault Filtering & Sorting State
  const [vaultSearch, setVaultSearch] = useState('');
  const [sortKey, setSortKey] = useState('avg');
  const [sortOrder, setSortOrder] = useState('desc');
  const [roleFilter, setRoleFilter] = useState('All');

  // AI Query State
  const [aiQuery, setAiQuery] = useState('');
  const [aiResult, setAiResult] = useState(null);
  const [isQuerying, setIsQuerying] = useState(false);

  // Rankings Logic
  const rankedPlayers = useMemo(() => {
    return [...INITIAL_PLAYERS].sort((a, b) => {
      const scoreA = (a.clutch * weights.clutch) + (a.sr * weights.sr) + (a.consistency * weights.consistency);
      const scoreB = (b.clutch * weights.clutch) + (b.sr * weights.sr) + (b.consistency * weights.consistency);
      return scoreB - scoreA;
    });
  }, [weights]);

  // Vault Logic (Filter & Sort)
  const filteredVault = useMemo(() => {
    return INITIAL_PLAYERS
      .filter(p => 
        (p.name.toLowerCase().includes(vaultSearch.toLowerCase()) || 
         p.country.toLowerCase().includes(vaultSearch.toLowerCase())) &&
        (roleFilter === 'All' || p.role === roleFilter)
      )
      .sort((a, b) => {
        const valA = a[sortKey];
        const valB = b[sortKey];
        // Handle string sorting (like names)
        if (typeof valA === 'string') {
          return sortOrder === 'asc' ? valA.localeCompare(valB) : valB.localeCompare(valA);
        }
        return sortOrder === 'asc' ? valA - valB : valB - valA;
      });
  }, [vaultSearch, sortKey, sortOrder, roleFilter]);

  const handleSort = (key) => {
    if (sortKey === key) {
      setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc');
    } else {
      setSortKey(key);
      setSortOrder('desc'); // Default to descending for new sorts (highest avg/sr first)
    }
  };

  const handleAiQuery = (e) => {
    e.preventDefault();
    if (!aiQuery.trim()) return;
    
    setIsQuerying(true);
    
    // Simulate network request/AI processing time
    setTimeout(() => {
      const queryLower = aiQuery.toLowerCase();
      let mockResponse = {};

      if (queryLower.includes('bumrah')) {
        mockResponse = {
          query: aiQuery,
          answer: "Jasprit Bumrah's death over economy rate (overs 17-20) against Left-Handed Batters is an exceptional 6.8 RPO over the last 24 months. He primarily relies on wide yorkers (42% usage) and slower off-cutters (28%).",
          recommendation: "Hold Bumrah back for the 18th and 20th overs if the opposition has LHB finishers at the crease.",
          stats: [
            { label: "Death Eco vs LHB", value: "6.80" },
            { label: "Dot Ball %", value: "38.5%" },
            { label: "Wicket Prob", value: "1 in 9 balls" }
          ]
        };
      } else if (queryLower.includes('rashid') || queryLower.includes('kohli')) {
        mockResponse = {
          query: aiQuery,
          answer: "In T20s, Rashid Khan has dismissed Virat Kohli 3 times in 82 deliveries. Kohli strikes at 112.5 against him, showing a cautious approach. Rashid predominantly bowls wrong'uns (60%) to Kohli early in his innings.",
          recommendation: "Introduce Rashid Khan immediately when Kohli arrives at the crease to drop his scoring rate.",
          stats: [
            { label: "Head-to-Head SR", value: "112.5" },
            { label: "Dismissals", value: "3" },
            { label: "False Shot %", value: "18.2%" }
          ]
        };
      } else {
        mockResponse = {
          query: aiQuery,
          answer: "Based on data from the last 3 seasons, Left-arm Pace (swing dominant) creates 22% more 'False Shots' against orthodox right-handed batters in overcast conditions.",
          recommendation: "Deploy a Left-arm seamer in the first 4 overs with a deep square leg and aggressive slip cordon.",
          stats: [
            { label: "False Shot %", value: "22.4%" },
            { label: "Avg Exit Vel", value: "88 mph" },
            { label: "Wicket Prob", value: "High" }
          ]
        };
      }

      setAiResult(mockResponse);
      setIsQuerying(false);
    }, 1200);
  };

  const togglePlayerSelection = (player) => {
    if (selectedPlayers.find(p => p.id === player.id)) {
      setSelectedPlayers(selectedPlayers.filter(p => p.id !== player.id));
    } else if (selectedPlayers.length < 11) {
      setSelectedPlayers([...selectedPlayers, player]);
    }
  };

  return (
    <div className="flex h-screen bg-[#F8FAFC] text-slate-800 overflow-hidden font-sans">
      {/* Sidebar */}
      <aside className="w-64 bg-white border-r border-slate-200 flex flex-col p-4 shadow-sm z-20">
        <div className="flex items-center space-x-2 px-4 mb-10 mt-2">
          <div className="bg-emerald-600 p-1.5 rounded-lg">
            <Target className="text-white" size={24} />
          </div>
          <h1 className="text-xl font-black tracking-tight text-slate-900">CRICLENS</h1>
        </div>

        <nav className="flex-1 space-y-2">
          <SidebarItem icon={Database} label="Player Vault" active={activeTab === 'vault'} onClick={() => setActiveTab('vault')} />
          <SidebarItem icon={Zap} label="Match-Up AI" active={activeTab === 'matchups'} onClick={() => setActiveTab('matchups')} />
          <SidebarItem icon={Users} label="Squad Scout" active={activeTab === 'rankings'} onClick={() => setActiveTab('rankings')} />
          <SidebarItem icon={Activity} label="Live Lab" active={activeTab === 'live'} onClick={() => setActiveTab('live')} />
        </nav>

        <div className="mt-auto p-4 bg-slate-50 rounded-xl border border-slate-100">
          <p className="text-[10px] text-slate-400 uppercase font-bold mb-1 tracking-widest">Account Status</p>
          <p className="text-sm font-bold text-slate-900">Pro Elite Member</p>
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 flex flex-col overflow-hidden relative">
        {/* Header */}
        <header className="h-16 border-b border-slate-200 bg-white flex items-center justify-between px-8 z-10">
          <div className="flex items-center flex-1 max-w-xl bg-slate-100 border border-slate-200 rounded-lg px-4 py-1.5">
            <Search size={18} className="text-slate-400" />
            <input type="text" placeholder="Global search..." className="bg-transparent border-none focus:ring-0 text-sm ml-2 w-full outline-none" />
          </div>
          <div className="h-9 w-9 rounded-full bg-slate-200 flex items-center justify-center text-slate-700 font-bold border border-slate-300 shadow-sm cursor-pointer hover:bg-slate-300 transition-colors">AD</div>
        </header>

        {/* View Switching */}
        <div className="flex-1 overflow-y-auto p-8 custom-scrollbar">
          
          {/* TAB: THE VAULT (DATABASE VIEW WITH FILTERS & SORTS) */}
          {activeTab === 'vault' && (
            <div className="animate-in slide-in-from-bottom-4 duration-500 max-w-6xl mx-auto">
              <div className="flex flex-col md:flex-row justify-between items-start md:items-center mb-8 gap-4">
                <div>
                  <h2 className="text-3xl font-black text-slate-900">Player Vault</h2>
                  <p className="text-slate-500 font-medium">Browse, filter, and sort the comprehensive player database.</p>
                </div>
                <div className="flex items-center space-x-3 w-full md:w-auto">
                   <div className="bg-white border border-slate-200 rounded-xl flex items-center px-4 py-2.5 shadow-sm flex-1 md:w-64">
                    <Search size={16} className="text-slate-400 mr-2 shrink-0" />
                    <input 
                      type="text" 
                      placeholder="Search name or nation..." 
                      className="border-none focus:ring-0 text-sm p-0 w-full outline-none bg-transparent"
                      value={vaultSearch}
                      onChange={(e) => setVaultSearch(e.target.value)}
                    />
                  </div>
                </div>
              </div>

              {/* Filters Section */}
              <div className="mb-6 flex items-center space-x-2 overflow-x-auto pb-2 no-scrollbar">
                <span className="text-xs font-bold text-slate-400 uppercase tracking-wider mr-2">Role:</span>
                {ROLES.map(role => (
                  <button
                    key={role}
                    onClick={() => setRoleFilter(role)}
                    className={`px-4 py-1.5 rounded-full text-xs font-bold transition-all whitespace-nowrap ${
                      roleFilter === role 
                        ? 'bg-emerald-600 text-white shadow-sm' 
                        : 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50 hover:border-slate-300'
                    }`}
                  >
                    {role}
                  </button>
                ))}
              </div>

              {/* Data Table */}
              <div className="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden">
                <table className="w-full text-left border-collapse">
                  <thead className="text-[10px] uppercase font-black text-slate-400 tracking-widest border-b border-slate-200 bg-slate-50">
                    <tr>
                      <th className="px-6 py-4 cursor-pointer hover:bg-slate-100 transition-colors group" onClick={() => handleSort('name')}>
                        <div className="flex items-center">
                          Player Name
                          {sortKey === 'name' ? (sortOrder === 'asc' ? <ArrowUp size={12} className="ml-1 text-emerald-600" /> : <ArrowDown size={12} className="ml-1 text-emerald-600" />) : <ArrowUpDown size={12} className="ml-1 opacity-0 group-hover:opacity-100 transition-opacity" />}
                        </div>
                      </th>
                      <th className="px-6 py-4 cursor-pointer hover:bg-slate-100 transition-colors group" onClick={() => handleSort('country')}>
                         <div className="flex items-center">
                          Nation
                          {sortKey === 'country' ? (sortOrder === 'asc' ? <ArrowUp size={12} className="ml-1 text-emerald-600" /> : <ArrowDown size={12} className="ml-1 text-emerald-600" />) : <ArrowUpDown size={12} className="ml-1 opacity-0 group-hover:opacity-100 transition-opacity" />}
                        </div>
                      </th>
                      <th className="px-6 py-4 cursor-pointer hover:bg-slate-100 transition-colors group" onClick={() => handleSort('matches')}>
                        <div className="flex items-center">
                          Caps
                          {sortKey === 'matches' ? (sortOrder === 'asc' ? <ArrowUp size={12} className="ml-1 text-emerald-600" /> : <ArrowDown size={12} className="ml-1 text-emerald-600" />) : <ArrowUpDown size={12} className="ml-1 opacity-0 group-hover:opacity-100 transition-opacity" />}
                        </div>
                      </th>
                      <th className="px-6 py-4 cursor-pointer hover:bg-slate-100 transition-colors group" onClick={() => handleSort('avg')}>
                        <div className="flex items-center">
                          Average
                          {sortKey === 'avg' ? (sortOrder === 'asc' ? <ArrowUp size={12} className="ml-1 text-emerald-600" /> : <ArrowDown size={12} className="ml-1 text-emerald-600" />) : <ArrowUpDown size={12} className="ml-1 opacity-0 group-hover:opacity-100 transition-opacity" />}
                        </div>
                      </th>
                      <th className="px-6 py-4 cursor-pointer hover:bg-slate-100 transition-colors group" onClick={() => handleSort('sr')}>
                        <div className="flex items-center">
                          Strike Rate
                          {sortKey === 'sr' ? (sortOrder === 'asc' ? <ArrowUp size={12} className="ml-1 text-emerald-600" /> : <ArrowDown size={12} className="ml-1 text-emerald-600" />) : <ArrowUpDown size={12} className="ml-1 opacity-0 group-hover:opacity-100 transition-opacity" />}
                        </div>
                      </th>
                      <th className="px-6 py-4">Role</th>
                      <th className="px-6 py-4 text-center">Action</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 text-sm">
                    {filteredVault.length === 0 ? (
                      <tr>
                        <td colSpan="7" className="px-6 py-12 text-center text-slate-400 font-medium">
                          No players found matching your criteria.
                        </td>
                      </tr>
                    ) : (
                      filteredVault.map(player => (
                        <tr key={player.id} className="hover:bg-slate-50 transition-colors">
                          <td className="px-6 py-4">
                            <div className="flex items-center space-x-3">
                              <div className="w-8 h-8 rounded-full bg-emerald-100 text-emerald-700 flex items-center justify-center text-[10px] font-black border border-emerald-200">
                                {player.image}
                              </div>
                              <span className="font-black text-slate-900">{player.name}</span>
                            </div>
                          </td>
                          <td className="px-6 py-4 font-bold text-slate-500">{player.country}</td>
                          <td className="px-6 py-4 font-mono text-slate-600">{player.matches}</td>
                          <td className="px-6 py-4 font-black text-slate-900">{player.avg.toFixed(1)}</td>
                          <td className="px-6 py-4 font-black text-emerald-600">{player.sr.toFixed(1)}</td>
                          <td className="px-6 py-4">
                            <span className="text-[10px] px-2.5 py-1 bg-slate-100 border border-slate-200 rounded-md font-bold text-slate-600 whitespace-nowrap">
                              {player.role}
                            </span>
                          </td>
                          <td className="px-6 py-4 text-center">
                            <button 
                              onClick={() => togglePlayerSelection(player)} 
                              className={`p-1.5 rounded-md border transition-all ${selectedPlayers.find(p => p.id === player.id) ? 'bg-rose-50 text-rose-600 border-rose-200' : 'bg-white text-slate-400 border-slate-200 hover:border-emerald-300 hover:text-emerald-600'}`}
                              title={selectedPlayers.find(p => p.id === player.id) ? "Remove from Squad" : "Add to Squad"}
                            >
                              {selectedPlayers.find(p => p.id === player.id) ? <Trash2 size={14} /> : <Plus size={14} />}
                            </button>
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* TAB: MATCH-UP LAB (NATURAL LANGUAGE QUERY) */}
          {activeTab === 'matchups' && (
            <div className="max-w-4xl mx-auto py-8 lg:py-12 animate-in fade-in zoom-in duration-500">
              <div className="text-center mb-10">
                <div className="inline-flex items-center space-x-2 bg-indigo-50 text-indigo-700 px-4 py-2 rounded-full mb-4 border border-indigo-100">
                  <Sparkles size={16} />
                  <span className="text-xs font-black uppercase tracking-widest">Natural Language Intelligence</span>
                </div>
                <h2 className="text-4xl lg:text-5xl font-black text-slate-900 mb-4 tracking-tight">Ask the Database</h2>
                <p className="text-slate-500 text-lg font-medium max-w-2xl mx-auto">Type your cricket analytics queries in plain English. The AI engine parses historical trends, match-ups, and situational data.</p>
              </div>

              <form onSubmit={handleAiQuery} className="relative mb-12">
                <div className="absolute inset-y-0 left-6 flex items-center pointer-events-none">
                  <MessageSquare className="text-slate-400" size={24} />
                </div>
                <input 
                  type="text" 
                  value={aiQuery}
                  onChange={(e) => setAiQuery(e.target.value)}
                  placeholder="e.g. 'Death over economy of Bumrah vs LHB'"
                  className="w-full bg-white border-2 border-slate-200 rounded-3xl py-5 lg:py-6 pl-16 pr-36 text-base lg:text-lg shadow-xl shadow-slate-200/40 focus:ring-4 focus:ring-emerald-500/10 focus:border-emerald-500 outline-none transition-all placeholder:text-slate-400"
                  disabled={isQuerying}
                />
                <button 
                  type="submit"
                  disabled={isQuerying || !aiQuery.trim()}
                  className={`absolute right-3 lg:right-4 inset-y-3 lg:inset-y-4 text-white font-black px-6 lg:px-8 rounded-2xl transition-all flex items-center space-x-2 shadow-lg ${isQuerying || !aiQuery.trim() ? 'bg-slate-400 cursor-not-allowed' : 'bg-slate-900 hover:bg-emerald-600 active:scale-95'}`}
                >
                  <span>{isQuerying ? 'Searching...' : 'Query'}</span>
                  {!isQuerying && <ChevronRight size={18} />}
                </button>
              </form>

              {isQuerying ? (
                <div className="flex flex-col items-center justify-center py-12 text-slate-400 animate-pulse">
                  <Database size={40} className="mb-4 text-emerald-500 opacity-50" />
                  <p className="font-bold uppercase tracking-widest text-xs">Querying Global Database...</p>
                </div>
              ) : aiResult ? (
                <div className="bg-white border border-slate-200 rounded-3xl p-6 lg:p-8 shadow-sm animate-in slide-in-from-top-4 duration-700">
                  <div className="flex items-center space-x-2 text-slate-400 mb-6 border-b border-slate-100 pb-4">
                    <Database size={14} />
                    <span className="text-xs font-bold uppercase tracking-tight">Analytical Insights for: "{aiResult.query}"</span>
                  </div>
                  
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-4 lg:gap-6 mb-8">
                    {aiResult.stats.map((stat, idx) => (
                      <div key={idx} className="bg-slate-50 p-5 rounded-2xl border border-slate-100 text-center">
                        <p className="text-[10px] font-black text-slate-400 uppercase mb-1 tracking-widest">{stat.label}</p>
                        <p className="text-2xl font-black text-slate-900">{stat.value}</p>
                      </div>
                    ))}
                  </div>

                  <div className="mb-8">
                    <h4 className="text-sm font-black text-slate-900 mb-4 flex items-center">
                      <Zap size={16} className="text-yellow-500 mr-2" /> Synthesis Result
                    </h4>
                    <p className="text-slate-600 leading-relaxed bg-indigo-50/20 p-6 rounded-2xl border border-indigo-100/30 italic font-medium">
                      "{aiResult.answer}"
                    </p>
                  </div>

                  <div className="bg-emerald-50 p-5 lg:p-6 rounded-2xl border border-emerald-100 flex items-start space-x-4">
                    <div className="bg-emerald-600 p-2 rounded-lg mt-1 shrink-0">
                      <Info size={16} className="text-white" />
                    </div>
                    <div>
                      <h4 className="text-sm font-black text-emerald-800 mb-1 uppercase tracking-tight">Scouting Action</h4>
                      <p className="text-emerald-700 font-bold text-sm leading-snug">{aiResult.recommendation}</p>
                    </div>
                  </div>
                  
                  <div className="mt-6 text-center">
                    <button onClick={() => setAiResult(null)} className="text-xs font-bold text-slate-400 hover:text-slate-600 uppercase tracking-widest">
                      Clear Results
                    </button>
                  </div>
                </div>
              ) : (
                <div>
                  <p className="text-xs font-black text-slate-400 uppercase tracking-widest mb-4 ml-2">Try these sample queries:</p>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 lg:gap-4">
                    {[
                      '"Death over economy of Bumrah vs LHB"', 
                      '"Rashid Khan vs Kohli: Ball by Ball Breakdown"', 
                      '"Highest Powerplay SR at Mumbai (2023)"', 
                      '"Impact of dew on chasing teams in Ahmedabad"'
                    ].map(hint => (
                      <button 
                        key={hint} 
                        onClick={() => setAiQuery(hint.replace(/"/g, ''))}
                        className="text-left bg-white border border-slate-200 p-4 lg:p-5 rounded-2xl text-xs lg:text-sm font-bold text-slate-500 hover:border-emerald-400 hover:text-emerald-600 hover:shadow-md transition-all group"
                      >
                        <span className="flex items-center">
                          <span className="flex-1">{hint}</span>
                          <ChevronRight size={14} className="opacity-0 group-hover:opacity-100 -translate-x-2 group-hover:translate-x-0 transition-all text-emerald-500" />
                        </span>
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* TAB: SQUAD SCOUT (ORIGINAL RANKING VIEW) */}
          {activeTab === 'rankings' && (
            <div className="animate-in fade-in duration-500 max-w-6xl mx-auto">
               <div className="flex justify-between items-end mb-8">
                <div>
                  <h2 className="text-3xl font-black text-slate-900 mb-2">Squad Scout</h2>
                  <p className="text-slate-500 max-w-lg font-medium">Fine-tune performance metrics to identify high-impact selections.</p>
                </div>
              </div>
              <div className="grid grid-cols-12 gap-8">
                <div className="col-span-12 lg:col-span-3 space-y-6">
                  <div className="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm sticky top-0">
                    <h3 className="text-[11px] font-black text-slate-400 uppercase tracking-widest mb-8">Performance Weights</h3>
                    <div className="space-y-10">
                      {Object.keys(weights).map(key => (
                        <div key={key}>
                          <div className="flex justify-between text-xs font-bold mb-4 uppercase">
                            <span className="text-slate-600">{key === 'sr' ? 'Strike Rate' : key}</span>
                            <span className="text-emerald-600">{weights[key]}%</span>
                          </div>
                          <input 
                            type="range" className="w-full accent-emerald-600 h-1.5 bg-slate-100 rounded-lg appearance-none cursor-pointer" 
                            value={weights[key]} 
                            onChange={(e) => setWeights({...weights, [key]: parseInt(e.target.value)})} 
                          />
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
                <div className="col-span-12 lg:col-span-9 bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden">
                  <table className="w-full text-left">
                    <thead className="bg-slate-50 text-slate-400 text-[10px] uppercase font-black tracking-widest border-b border-slate-200">
                      <tr>
                        <th className="px-6 py-5">Rank</th>
                        <th className="px-6 py-5">Player</th>
                        <th className="px-6 py-5">Avg / SR</th>
                        <th className="px-6 py-5">Efficiency Score</th>
                        <th className="px-6 py-5 text-center">Add</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {rankedPlayers.map((player, index) => (
                        <tr key={player.id} className="hover:bg-slate-50 transition-colors">
                          <td className="px-6 py-5 font-black text-emerald-600">#{index+1}</td>
                          <td className="px-6 py-5">
                            <p className="text-sm font-black text-slate-900 leading-tight">{player.name}</p>
                            <p className="text-[10px] text-slate-400 font-bold uppercase mt-1">{player.country} • {player.role}</p>
                          </td>
                          <td className="px-6 py-5">
                            <p className="text-sm font-black text-slate-900">{player.avg.toFixed(1)}</p>
                            <p className="text-[10px] text-emerald-600 font-bold">{player.sr.toFixed(1)} SR</p>
                          </td>
                          <td className="px-6 py-5">
                            <div className="flex items-center space-x-3">
                              <div className="h-2 w-20 bg-slate-100 rounded-full overflow-hidden">
                                <div className="h-full bg-emerald-600" style={{ width: `${(player.clutch + player.consistency)/2}%` }}></div>
                              </div>
                              <span className="text-xs font-black text-slate-700">{Math.round((player.clutch * weights.clutch + player.sr * 0.5 * weights.sr) / 100 + 20)}</span>
                            </div>
                          </td>
                          <td className="px-6 py-5 text-center">
                            <button onClick={() => togglePlayerSelection(player)} className={`p-2 rounded-lg border transition-all ${selectedPlayers.find(p => p.id === player.id) ? 'bg-rose-50 text-rose-600 border-rose-200' : 'bg-white text-slate-400 border-slate-200 hover:border-emerald-300 hover:text-emerald-600'}`}>
                              {selectedPlayers.find(p => p.id === player.id) ? <Trash2 size={16} /> : <Plus size={16} />}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}

          {/* TAB: LIVE LAB (PLACEHOLDER) */}
          {activeTab === 'live' && (
             <div className="flex flex-col items-center justify-center h-full text-center text-slate-400 animate-in fade-in">
                <Activity size={48} className="mb-4 text-slate-300" />
                <h3 className="text-xl font-black text-slate-600 mb-2">Live Match Lab Offline</h3>
                <p className="text-sm font-medium">Connects to live API streams during active match hours.</p>
             </div>
          )}

        </div>

        {/* Squad Selection Tray (Persistent) */}
        <div className={`fixed bottom-6 right-6 left-70 bg-white border border-slate-200 rounded-2xl shadow-[0_10px_40px_-10px_rgba(0,0,0,0.1)] transform transition-all duration-500 ease-out z-30 ${selectedPlayers.length > 0 ? 'translate-y-0 opacity-100' : 'translate-y-20 opacity-0 pointer-events-none'}`} style={{ left: 'calc(16rem + 2rem)' }}>
          <div className="px-6 py-4 lg:px-8 lg:py-5 flex items-center justify-between">
            <div className="flex items-center space-x-6 lg:space-x-8">
              <div>
                <p className="text-[10px] font-black text-slate-400 uppercase tracking-widest mb-1">Squad Draft</p>
                <div className="flex items-baseline space-x-2">
                  <span className="text-2xl font-black text-slate-900">{selectedPlayers.length}</span>
                  <span className="text-slate-400 font-bold text-sm">/ 11</span>
                </div>
              </div>
              <div className="flex -space-x-2.5 overflow-hidden py-1">
                {selectedPlayers.map(p => (
                  <div key={p.id} title={p.name} className="w-10 h-10 lg:w-11 lg:h-11 rounded-xl bg-emerald-600 border-4 border-white flex items-center justify-center text-[10px] font-black text-white shadow-md ring-1 ring-slate-100 shrink-0">
                    {p.image}
                  </div>
                ))}
              </div>
            </div>
            <div className="flex items-center space-x-2 lg:space-x-4">
              <button 
                onClick={() => setSelectedPlayers([])}
                className="px-3 py-2 text-[10px] lg:text-xs font-black text-slate-400 hover:text-rose-600 transition-colors uppercase tracking-widest"
              >
                Clear
              </button>
              <button className="bg-slate-900 text-white font-black px-6 py-2.5 lg:px-8 lg:py-3 rounded-xl text-xs lg:text-sm shadow-lg hover:bg-emerald-600 transition-all active:scale-95">
                Lock Squad
              </button>
            </div>
          </div>
        </div>
      </main>

      <style>{`
        .custom-scrollbar::-webkit-scrollbar { width: 6px; }
        .custom-scrollbar::-webkit-scrollbar-track { background: transparent; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 10px; }
        .custom-scrollbar::-webkit-scrollbar-thumb:hover { background: #94a3b8; }
        .no-scrollbar::-webkit-scrollbar { display: none; }
      `}</style>
    </div>
  );
};

export default App;