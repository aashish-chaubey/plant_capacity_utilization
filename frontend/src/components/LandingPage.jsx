import React from 'react';
import { Clock } from 'lucide-react';
import './LandingPage.css';

// Import plant monument SVG icons
import ahmSvg from '../../images/svg_icons/AHM.svg';
import airSvg from '../../images/svg_icons/AIR.svg';
import barSvg from '../../images/svg_icons/BAR.svg';
import blrSvg from '../../images/svg_icons/BLR.svg';
import cheSvg from '../../images/svg_icons/CHE.svg';
import hydSvg from '../../images/svg_icons/HYD.svg';
import kanSvg from '../../images/svg_icons/KAN.svg';
import kolSvg from '../../images/svg_icons/KOL.svg';
import lucSvg from '../../images/svg_icons/LUC.svg';
import msrSvg from '../../images/svg_icons/MSR.svg';
import nagSvg from '../../images/svg_icons/NAG.svg';
import punSvg from '../../images/svg_icons/PUN.svg';
import sbdSvg from '../../images/svg_icons/SBD.svg';
import tvmSvg from '../../images/svg_icons/TVM.svg';
import machineImg from '../../images/KAN/machine_img.png';
import logoImg from '../../images/KAN/logo.png';

const DESIGN_PLANTS = [
  { key: "Sahibabad", label: "SAHIBABAD", img: sbdSvg },
  { key: "Manesar", label: "MANESAR", img: msrSvg },
  { key: "Kandivali", label: "KANDIVALI", img: kanSvg },
  { key: "Airoli", label: "AIROLI", img: airSvg },
  { key: "Bommasandra", label: "BANGALORE", img: blrSvg },
  { key: "Chemmencherry", label: "CHENNAI", img: cheSvg },
  { key: "Saltlake", label: "KOLKATA", img: kolSvg },
  { key: "Bhosari", label: "PUNE", img: punSvg },
  { key: "Nacharam", label: "HYDERABAD", img: hydSvg },
  { key: "Vejalpur", label: "AHMEDABAD", img: ahmSvg },
  { key: "Baroda", label: "BARODA", img: barSvg },
  { key: "Chinhat", label: "LUCKNOW", img: lucSvg },
  { key: "Butibori", label: "NAGPUR", img: nagSvg },
  { key: "Trivandrum", label: "TRIVANDRAM", img: tvmSvg }
];

export default function LandingPage({ onSelectPlant }) {
  return (
    <div
      className="landing-page-container flex-1 flex-col items-center justify-start py-12 px-6 sm:px-12 lg:px-20 relative"
      style={{ backgroundColor: '#1f1e45' }}
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
        <div className="flex items-center select-none pace-logo-wrap">
          <img
            src={logoImg}
            alt="PACE Logo"
            className="h-[60px] sm:h-[75px] lg:h-[85px] w-auto object-contain"
          />
        </div>

        {/* Subtitle */}
        <h2 className="text-xl sm:text-2xl lg:text-[26px] font-medium text-white/95 mt-4 tracking-wide leading-snug">
          Plant Analytics for Capacity Evaluation
        </h2>

        {/* Glowing Divider */}
        <div className="header-divider" />

        {/* Tagline */}
        <p className="text-[#38BDF8] font-normal text-sm sm:text-base tracking-wider pl-1 mt-2">
          AI-Powered Platform for Capacity Utilisation Analysis
        </p>
      </div>

      {/* Middle Section: 3 Info Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 max-w-[1360px] w-full" style={{ marginTop: 'clamp(70px, 18vh, 250px)', marginBottom: 'clamp(12px, 2vh, 30px)' }}>
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
          <div className="plants-row-container flex items-end justify-between w-full px-2 gap-4">
            {DESIGN_PLANTS.map((plant) => (
              <button
                key={plant.key}
                type="button"
                onClick={() => onSelectPlant(plant.key)}
                className="plant-item-btn flex flex-col items-center flex-1 py-1 px-1 rounded-xl text-center group cursor-pointer focus:outline-none focus:ring-2 focus:ring-blue-100"
              >
                <div className="plant-image-wrapper flex flex-col items-center justify-center m-0 p-0">
                  <img
                    src={plant.img}
                    alt={plant.label}
                    className="max-h-full max-w-full object-contain filter drop-shadow-sm select-none m-0 p-0"
                  />
                  <span className="plant-label-text text-[8px] sm:text-[9px] font-bold text-slate-800 tracking-wider m-0 p-0 mt-0 mb-1.5 leading-none select-none transition-colors group-hover:text-blue-600">
                    {plant.label}
                  </span>
                </div>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

