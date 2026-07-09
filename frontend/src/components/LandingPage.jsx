import React from 'react';
import { Clock } from 'lucide-react';
import './LandingPage.css';

// Import plant monument icons
import ahmImg from '../../images/KAN/AHM.png';
import airImg from '../../images/KAN/AIR.png';
import barImg from '../../images/KAN/BAR.png';
import blrImg from '../../images/KAN/BLR.png';
import cheImg from '../../images/KAN/CHE.png';
import hydImg from '../../images/KAN/HYD.png';
import kanImg from '../../images/KAN/KAN.png';
import kolImg from '../../images/KAN/KOL.png';
import lucImg from '../../images/KAN/LUC.png';
import msrImg from '../../images/KAN/MSR.png';
import nagImg from '../../images/KAN/NAG.png';
import punImg from '../../images/KAN/PUN.png';
import sbdImg from '../../images/KAN/SBD.png';
import tvmImg from '../../images/KAN/TVM.png';
import machineImg from '../../images/KAN/machine_img.png';

const DESIGN_PLANTS = [
  { key: "Vejalpur", label: "AHMEDABAD", img: ahmImg },
  { key: "Airoli", label: "AIROLI", img: airImg },
  { key: "Bommasandra", label: "BANGALORE", img: blrImg },
  { key: "Baroda", label: "BARODA", img: barImg },
  { key: "Chemmencherry", label: "CHENNAI", img: cheImg },
  { key: "Nacharam", label: "HYDERABAD", img: hydImg },
  { key: "Kandivali", label: "KANDIVALI", img: kanImg },
  { key: "Saltlake", label: "KOLKATA", img: kolImg },
  { key: "Chinhat", label: "LUCKNOW", img: lucImg },
  { key: "Manesar", label: "MANESAR", img: msrImg },
  { key: "Butibori", label: "NAGPUR", img: nagImg },
  { key: "Bhosari", label: "PUNE", img: punImg },
  { key: "Sahibabad", label: "SAHIBABAD", img: sbdImg },
  { key: "Trivandrum", label: "TRIVANDRAM", img: tvmImg }
];

export default function LandingPage({ onSelectPlant }) {
  return (
    <div
      className="landing-page-container flex-1 flex-col items-center justify-start py-12 px-6 sm:px-12 lg:px-20 relative"
      style={{ backgroundColor: '#020813' }}
    >
      {/* Background machine image — absolutely positioned, native size capped, 15% fade on left & bottom */}
      <img
        src={machineImg}
        alt=""
        aria-hidden="true"
        className="machine-bg-img"
      />
      {/* Top Header Section */}
      <div className="max-w-[1360px] w-full mt-4 lg:mt-8">
        {/* PACE Logo */}
        <div className="flex items-center select-none">
          <span className="flex items-center font-black tracking-normal text-white text-6xl sm:text-7xl lg:text-[85px] leading-none">
            P
            <svg
              className="text-[#00E5FF] drop-shadow-[0_0_14px_rgba(0,229,255,0.9)] fill-current shrink-0 self-center"
              viewBox="0 0 80 100"
              style={{ display: 'inline-block', height: '0.92em', width: '0.7em', transform: 'translateY(-1%)', margin: '0 0.04em' }}
            >
              {/* Upward triangle representing letter A */}
              <polygon points="40,4 78,92 2,92" />
              {/* Inner cutout to mimic the crossbar of A */}
              <polygon points="40,4 78,92 2,92" fill="transparent" />
              <polygon points="24,70 56,70 62,84 18,84" fill="#020813" />
            </svg>
            CE
          </span>
        </div>

        {/* Subtitle */}
        <h2 className="text-xl sm:text-2xl lg:text-[26px] font-medium text-white/95 mt-4 tracking-wide leading-snug">
          Plant Analytics for Capacity Evaluation
        </h2>

        {/* Glowing Divider */}
        <div className="header-divider" />

        {/* Tagline */}
        <p className="text-[#38BDF8] font-bold text-sm sm:text-base tracking-wider uppercase pl-1 mt-3">
          AI-Powered Platform for Capacity Utilisation Analysis
        </p>
      </div>

      {/* Middle Section: 3 Info Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 max-w-[1360px] w-full" style={{ marginTop: 'clamp(100px, 13vh, 180px)', marginBottom: 'clamp(12px, 2vh, 30px)' }}>
        {/* Card 1 */}
        <div className="mockup-card mockup-card-blue">
          <div className="card-top-sec">
            <span className="text-[#38BDF8] text-[11px] sm:text-xs font-bold uppercase tracking-wider">
              Intelligent Insights on
            </span>
          </div>
          <div className="separator-blue" />
          <div className="card-bottom-sec">
            <span className="text-white text-lg sm:text-xl lg:text-[22px] font-black leading-tight">
              Tower / Folder<br />Utilisation
            </span>
          </div>
        </div>

        {/* Card 2 */}
        <div className="mockup-card mockup-card-green">
          <div className="card-top-sec">
            <span className="text-[#00FF87] text-[11px] sm:text-xs font-bold uppercase tracking-wider">
              Mastering the Clock
            </span>
          </div>
          <div className="separator-green" />
          <div className="card-bottom-sec">
            <span className="text-white text-lg sm:text-xl lg:text-[22px] font-black leading-tight">
              Capacity
            </span>
          </div>
        </div>

        {/* Card 3 */}
        <div className="mockup-card mockup-card-purple">
          <div className="card-top-sec flex items-center justify-center gap-1.5">
            <Clock className="w-4 h-4 text-[#D200FF]" />
            <span className="text-[#D200FF] text-[11px] sm:text-xs font-bold uppercase tracking-wider">
              Prime Print Window
            </span>
          </div>
          <div className="separator-purple" />
          <div className="card-bottom-sec">
            <span className="text-white text-lg sm:text-xl lg:text-[22px] font-black leading-tight">
              12:00 AM – 4:00 AM
            </span>
          </div>
        </div>
      </div>

      {/* Bottom Section: Plant Selection Banner */}
      <div className="bottom-selection-banner w-full max-w-[1360px] bg-white rounded-[16px] p-5 shadow-[0_20px_50px_rgba(0,0,0,0.3)] mb-2" style={{ marginTop: 0 }}>
        {/* Header */}
        <div className="text-center font-black text-slate-800 text-xs sm:text-sm tracking-[0.2em] mb-4">
          14 PLANTS. ONE PLATFORM.
        </div>

        {/* Divider */}
        <hr className="border-slate-200/80 mb-5" />

        {/* Horizontal Row of Plants */}
        <div className="plants-scrollbar overflow-x-auto w-full pb-2">
          <div className="plants-row-container flex items-end justify-between min-w-[1240px] px-2 gap-4">
            {DESIGN_PLANTS.map((plant) => (
              <button
                key={plant.key}
                type="button"
                onClick={() => onSelectPlant(plant.key)}
                className="plant-item-btn flex flex-col items-center flex-1 py-3 px-1 rounded-xl text-center group cursor-pointer focus:outline-none focus:ring-2 focus:ring-blue-100"
              >
                <div className="plant-image-wrapper flex items-center justify-center">
                  <img
                    src={plant.img}
                    alt={plant.label}
                    className="max-h-full max-w-full object-contain filter drop-shadow-sm select-none"
                  />
                </div>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
