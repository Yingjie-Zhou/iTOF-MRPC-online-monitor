#include "TApplication.h"
#include "TArrayD.h"
#include "TAxis.h"
#include "TColor.h"
#include "TFile.h"
#include "TGeoBBox.h"
#include "TGeoMatrix.h"
#include "TGeoManager.h"
#include "TGeoMaterial.h"
#include "TGeoMedium.h"
#include "TGeoVolume.h"
#include "TH1.h"
#include "TH1D.h"
#include "TH2.h"
#include "TH2D.h"
#include "TH3D.h"
#include "THttpServer.h"
#include "TMarker3DBox.h"
#include "TMath.h"
#include "TObjArray.h"
#include "TPolyMarker3D.h"
#include "TROOT.h"
#include "TStyle.h"
#include "TString.h"
#include "TSystem.h"
#include "TText.h"
#include "TVector3.h"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <memory>
#include <set>
#include <sstream>
#include <string>
#include <sys/stat.h>
#include <thread>
#include <unordered_map>
#include <vector>

namespace {

const double kStripHalfLengthCm = 21.;
const double kStripLengthCm = 2. * kStripHalfLengthCm;
const double kSignalVelocityCmPerNs = 16.;
const double kEventDisplayLayerSpacingCm = 17.;

struct EleMapEntry {
  int det = -1;
  int strip = -1;
  int side = -1;
  EleMapEntry() {}
  EleMapEntry(int d, int s, int sd) : det(d), strip(s), side(sd) {}
};

struct RawDigi {
  int det = -1;
  int strip = -1;
  int side = -1;
  int fee = -1;
  int channel = -1;
  int triggerId = 0;
  double time = 0.;
  double tot = 0.;
  double leadingRelNs = 0.;
  double doubleChainDiffPs = 0.;
  RawDigi() {}
  RawDigi(int d, int st, int sd, int f, int ch, int trig, double t, double q, double leadRel, double chainDiff)
    : det(d), strip(st), side(sd), fee(f), channel(ch), triggerId(trig), time(t), tot(q),
      leadingRelNs(leadRel), doubleChainDiffPs(chainDiff) {}
};

struct OnlineHit {
  int det = -1;
  int strip = -1;
  int triggerId = 0;
  double x = 0.;
  double y = 0.;
  double z = 0.;
  double time = 0.;
  double tot = 0.;
  double rawTimeDiffNs = 0.;
  OnlineHit() {}
  OnlineHit(int d, int st, int trig, double xx, double yy, double zz, double t, double q, double rawDiff)
    : det(d), strip(st), triggerId(trig), x(xx), y(yy), z(zz), time(t), tot(q), rawTimeDiffNs(rawDiff) {}
};

struct EventHitCluster {
  int det = -1;
  int firstStrip = -1;
  int lastStrip = -1;
  double x = 0.;
  double y = 0.;
  double z = 0.;
  double weight = 0.;
  int count = 0;
};

struct TrackFitResult {
  std::vector<TVector3> line;
  std::vector<size_t> inliers;
  double chi2 = 1.e300;
  bool reliable = false;
};

struct FeeHeader {
  uint64_t id = 0;
  uint64_t dataLength = 0;
  uint64_t address = 0;
};

struct AssembleHeader {
  uint64_t id = 0;
  uint64_t length = 0;
  uint64_t feeNum = 0;
  std::vector<FeeHeader> fees;
};

struct EventHeader {
  uint64_t head = 0;
  uint64_t length = 0;
  uint64_t triggerId = 0;
  uint64_t assembleNum = 0;
  std::vector<AssembleHeader> assembles;
};

std::string trim(const std::string& input)
{
  const size_t first = input.find_first_not_of(" \t\r\n");
  if (first == std::string::npos) return "";
  const size_t last = input.find_last_not_of(" \t\r\n");
  return input.substr(first, last - first + 1);
}

std::string expandEnv(std::string value)
{
  size_t pos = value.find("$VMCWORKDIR");
  if (pos != std::string::npos) {
    const char* vmc = std::getenv("VMCWORKDIR");
    if (vmc) value.replace(pos, 11, vmc);
  }
  return value;
}

bool isDir(const std::string& path)
{
  struct stat st;
  std::memset(&st, 0, sizeof(st));
  return stat(path.c_str(), &st) == 0 && S_ISDIR(st.st_mode);
}

bool isFile(const std::string& path)
{
  struct stat st;
  std::memset(&st, 0, sizeof(st));
  return stat(path.c_str(), &st) == 0 && S_ISREG(st.st_mode);
}

Long64_t fileSize(const std::string& path)
{
  struct stat st;
  std::memset(&st, 0, sizeof(st));
  if (stat(path.c_str(), &st) != 0) return -1;
  return static_cast<Long64_t>(st.st_size);
}

bool endsWith(const std::string& value, const std::string& suffix)
{
  return value.size() >= suffix.size() &&
         value.compare(value.size() - suffix.size(), suffix.size(), suffix) == 0;
}

std::vector<std::string> readListFile(const std::string& listFile)
{
  std::vector<std::string> files;
  std::ifstream in(listFile);
  std::string line;
  while (std::getline(in, line)) {
    line = trim(line);
    if (line.empty() || line[0] == '#') continue;
    files.push_back(expandEnv(line));
  }
  return files;
}

void collectDatFiles(const std::string& dir, std::vector<std::string>& files)
{
  DIR* dp = opendir(dir.c_str());
  if (!dp) return;
  while (dirent* entry = readdir(dp)) {
    const std::string name = entry->d_name;
    if (name == "." || name == "..") continue;
    const std::string path = dir + "/" + name;
    if (isDir(path)) {
      collectDatFiles(path, files);
    } else if (isFile(path) && endsWith(path, ".dat")) {
      files.push_back(path);
    }
  }
  closedir(dp);
}

std::vector<std::string> resolveInputFiles(const std::string& input)
{
  std::vector<std::string> files;
  const std::string expanded = expandEnv(input);
  if (isDir(expanded)) {
    collectDatFiles(expanded, files);
  } else if (endsWith(expanded, ".list")) {
    files = readListFile(expanded);
  } else {
    files.push_back(expanded);
  }
  std::sort(files.begin(), files.end());
  return files;
}

uint64_t readUIntLE(std::istream& in, int nBytes)
{
  uint64_t value = 0;
  char buffer[8] {};
  in.read(buffer, nBytes);
  if (!in) return 0;
  std::memcpy(&value, buffer, nBytes);
  return value;
}

std::vector<uint64_t> defaultAssembleIds()
{
  return {0x300};
}

void configureOnlineRootStyle()
{
  static bool configured = false;
  if (configured) return;
  configured = true;

  const Int_t nStops = 5;
  Double_t stops[nStops] = {0.00, 0.25, 0.50, 0.75, 1.00};
  Double_t red[nStops]   = {0.03, 0.05, 0.02, 0.15, 0.95};
  Double_t green[nStops] = {0.10, 0.32, 0.72, 0.88, 0.95};
  Double_t blue[nStops]  = {0.28, 0.80, 0.86, 0.35, 0.08};
  TColor::CreateGradientColorTable(nStops, stops, red, green, blue, 96);

  gStyle->SetNumberContours(96);
  gStyle->SetOptStat(0);
  gStyle->SetOptTitle(0);
  gStyle->SetTitleBorderSize(0);
  gStyle->SetTitleFillColor(0);
  gStyle->SetTitleTextColor(TColor::GetColor("#0f172a"));
  gStyle->SetTextFont(42);
  gStyle->SetStatFont(42);
  gStyle->SetLegendFont(42);
  gStyle->SetTitleFont(42, "XYZ");
  gStyle->SetLabelFont(42, "XYZ");
  gStyle->SetFrameLineColor(TColor::GetColor("#334155"));
  gStyle->SetFrameLineWidth(1);
  gStyle->SetPadColor(0);
  gStyle->SetCanvasColor(0);
  gStyle->SetPadGridX(true);
  gStyle->SetPadGridY(true);
  gStyle->SetGridColor(TColor::GetColor("#d7e0ea"));
  gStyle->SetGridStyle(3);
  gStyle->SetGridWidth(1);
  gStyle->SetPadTickX(1);
  gStyle->SetPadTickY(1);
  gStyle->SetPadLeftMargin(0.11);
  gStyle->SetPadRightMargin(0.13);
  gStyle->SetPadTopMargin(0.08);
  gStyle->SetPadBottomMargin(0.12);
}

void styleAxis(TAxis* axis)
{
  if (!axis) return;
  axis->SetTitleFont(42);
  axis->SetLabelFont(42);
  axis->SetTitleSize(0.040);
  axis->SetLabelSize(0.032);
  axis->SetTitleColor(TColor::GetColor("#17202e"));
  axis->SetLabelColor(TColor::GetColor("#334155"));
  axis->SetAxisColor(TColor::GetColor("#475569"));
}

void styleOnlineHist1D(TH1* hist)
{
  if (!hist) return;
  hist->SetStats(false);
  hist->SetLineColor(kAzure + 4);
  hist->SetLineWidth(1);
  hist->SetFillColor(TColor::GetColor("#dbeafe"));
  hist->SetFillStyle(1001);
  hist->SetMarkerColor(kAzure + 4);
  hist->SetTitleFont(42);
  hist->SetTitleSize(0.045);
  styleAxis(hist->GetXaxis());
  styleAxis(hist->GetYaxis());
}

void styleOnlineHist2D(TH2* hist)
{
  if (!hist) return;
  hist->SetStats(false);
  hist->SetContour(96);
  hist->SetLineColor(kAzure + 4);
  hist->SetMarkerColor(kAzure + 4);
  hist->SetTitleFont(42);
  hist->SetTitleSize(0.045);
  styleAxis(hist->GetXaxis());
  styleAxis(hist->GetYaxis());
  styleAxis(hist->GetZaxis());
}

double clsbCorrection(const std::unordered_map<uint64_t, std::vector<double>>& clsb,
                      uint64_t key,
                      uint64_t precise)
{
  const auto it = clsb.find(key);
  if (it == clsb.end() || precise >= it->second.size()) return static_cast<double>(precise) / 170.;
  return it->second[precise];
}

std::unordered_map<int, EleMapEntry> loadEleMap(const std::string& path)
{
  std::unordered_map<int, EleMapEntry> out;
  std::ifstream in(expandEnv(path));
  if (!in) {
    std::cerr << "Cannot open electronics map: " << path << std::endl;
    return out;
  }
  std::string line;
  int lineNo = 0;
  while (std::getline(in, line)) {
    ++lineNo;
    if (lineNo < 4) continue;
    std::stringstream ss(line);
    std::string field;
    std::vector<int> values;
    while (std::getline(ss, field, ',')) values.push_back(std::atoi(field.c_str()));
    if (values.size() < 5) continue;
    const int fee = values[3];
    const int channel = values[4];
    out[fee * 100 + channel] = EleMapEntry(values[0], values[1], values[2]);
  }
  return out;
}

std::unordered_map<uint64_t, std::vector<double>> loadCLSB(const std::string& path)
{
  std::unordered_map<uint64_t, std::vector<double>> out;
  std::ifstream in(expandEnv(path));
  if (!in) {
    std::cerr << "Cannot open CLSB file: " << path << std::endl;
    return out;
  }
  std::string line;
  while (std::getline(in, line)) {
    const size_t keyPos = line.find("Key:");
    const size_t arrow = line.find("->");
    const size_t lb = line.find('[');
    const size_t rb = line.find(']');
    if (keyPos == std::string::npos || arrow == std::string::npos ||
        lb == std::string::npos || rb == std::string::npos || rb <= lb) {
      continue;
    }
    std::string keyText = trim(line.substr(keyPos + 4, arrow - keyPos - 4));
    uint64_t key = 0;
    std::stringstream keyStream;
    keyStream << std::hex << keyText;
    keyStream >> key;

    std::vector<double> values;
    std::stringstream valuesStream(line.substr(lb + 1, rb - lb - 1));
    std::string value;
    while (std::getline(valuesStream, value, ',')) values.push_back(std::atof(value.c_str()));
    if (!values.empty()) out[key] = values;
  }
  return out;
}

class NomagGeometry {
public:
  NomagGeometry()
  {
    smX = {-92.4, -82.8, -92.4, -54.4, 54.4, 92.4, 82.8, 92.4};
    smY = {-18.8, -18.8, -18.8, -18.8, -18.8, -18.8, -18.8, -18.8};
    smZ = {-30.2, -0.2, 34., 69., 69., 34., 1.7, -30.2};
    smRotation = {1, 1, 1, 3, 3, 0, 0, 0};
    rpcY = {34.21, 0., -34.21};
  }

  int nDet() const { return 24; }
  int nStrips(int) const { return 32; }

  TVector3 hitPosition(int det, int strip, double xLocalCm) const
  {
    const double pitchCm = 1.0;
    const double yLocalCm = (strip - (nStrips(det) - 1) / 2.) * pitchCm;
    return rpcCenter(det) + rotate(det / 3, TVector3(xLocalCm, yLocalCm, 0.), 1);
  }

  TVector3 localPosition(int det, const TVector3& global) const
  {
    const int sm = det / 3;
    return rotate(sm, global - rpcCenter(det), -1);
  }

  int wallGroup(int det) const
  {
    const int sm = det / 3;
    const int type = smRotation.at(sm);
    if (type == 1 || type == 3) return 1; // z wall
    return smX[sm] < 0. ? 0 : 2;          // x- or x+ wall
  }

  TVector3 detectorCenter(int det) const
  {
    return rpcCenter(det);
  }

  TGeoRotation rotation(int sm) const
  {
    const int type = smRotation.at(sm);
    if (type == 0) return TGeoRotation("rot0", 90., -90., -90.);
    if (type == 1) return TGeoRotation("rot1", 90., 90., -90.);
    if (type == 3) return TGeoRotation("rot3", 90., 180., -90.);
    return TGeoRotation("norot", 0., 0., 0.);
  }

  TVector3 rotate(int sm, const TVector3& vec, int direction) const
  {
    TGeoRotation rot = rotation(sm);
    const Double_t* matrix = rot.GetRotationMatrix();
    const double x = vec.X();
    const double y = vec.Y();
    const double z = vec.Z();
    if (direction == -1) {
      return TVector3(matrix[0] * x + matrix[3] * y + matrix[6] * z,
                      matrix[1] * x + matrix[4] * y + matrix[7] * z,
                      matrix[2] * x + matrix[5] * y + matrix[8] * z);
    }
    return TVector3(matrix[0] * x + matrix[1] * y + matrix[2] * z,
                    matrix[3] * x + matrix[4] * y + matrix[5] * z,
                    matrix[6] * x + matrix[7] * y + matrix[8] * z);
  }

private:
  TVector3 rpcCenter(int det) const
  {
    const int sm = det / 3;
    const int rpc = det % 3;
    return rotate(sm, TVector3(0., rpcY[rpc], 0.), 1) +
           TVector3(smX[sm], smY[sm], smZ[sm]);
  }

  std::vector<double> smX;
  std::vector<double> smY;
  std::vector<double> smZ;
  std::vector<int> smRotation;
  std::vector<double> rpcY;
};

struct CalibrationTable {
  bool loaded = false;
  bool selfAlign = false;
  double sigVel[24];
  double dt[24][32];
  Long64_t selfCount[24][32];
  double selfOffsetNs[24][32];
  double commonOffsetNs = 0.;
  double commonPositionCorrectionCm = 0.;
  Long64_t commonAlignCount = 0;
  std::vector<double> selfSamplesNs[24][32];
  std::vector<double> trackResidualSamplesCm[24][32];
  std::vector<double> centerSamplesCm[24];
  Long64_t trackAlignCount[24][32];

  CalibrationTable()
  {
    for (int det = 0; det < 24; ++det) {
      sigVel[det] = 16.;
      for (int strip = 0; strip < 32; ++strip) {
        dt[det][strip] = 0.;
        selfCount[det][strip] = 0;
        selfOffsetNs[det][strip] = 0.;
        trackAlignCount[det][strip] = 0;
      }
    }
  }

  static double median(std::vector<double> values)
  {
    if (values.empty()) return 0.;
    const size_t mid = values.size() / 2;
    std::nth_element(values.begin(), values.begin() + mid, values.end());
    double result = values[mid];
    if (values.size() % 2 == 0) {
      std::nth_element(values.begin(), values.begin() + mid - 1, values.end());
      result = 0.5 * (result + values[mid - 1]);
    }
    return result;
  }

  static double robustCenter(const std::vector<double>& samples)
  {
    if (samples.empty()) return 0.;
    const double med = median(samples);
    std::vector<double> absDev;
    absDev.reserve(samples.size());
    for (double value : samples) absDev.push_back(std::fabs(value - med));
    const double mad = median(absDev);
    const double window = std::max(1.5, 4.0 * 1.4826 * mad);
    double sum = 0.;
    int count = 0;
    for (double value : samples) {
      if (std::fabs(value - med) <= window) {
        sum += value;
        ++count;
      }
    }
    return count > 0 ? sum / static_cast<double>(count) : med;
  }

  static double quantile(std::vector<double> values, double q)
  {
    if (values.empty()) return 0.;
    std::sort(values.begin(), values.end());
    const double pos = std::max(0., std::min(1., q)) * static_cast<double>(values.size() - 1);
    const size_t low = static_cast<size_t>(std::floor(pos));
    const size_t high = static_cast<size_t>(std::ceil(pos));
    if (low == high) return values[low];
    const double frac = pos - static_cast<double>(low);
    return values[low] * (1. - frac) + values[high] * frac;
  }

  static bool firstValue(TFile& file, const char* name, double& value)
  {
    TArrayD* arr = nullptr;
    file.GetObject(name, arr);
    if (!arr || arr->GetSize() <= 0) return false;
    value = arr->At(0);
    return true;
  }

  bool load(const std::string& path)
  {
    TDirectory* oldDir = gDirectory;
    TFile file(expandEnv(path).c_str(), "READ");
    if (!file.IsOpen() || file.IsZombie()) {
      std::cerr << "Calibration file is not available; online hits use default alignment: "
                << path << std::endl;
      return false;
    }

    loaded = true;
    for (int det = 0; det < 24; ++det) {
      for (int strip = 0; strip < 32; ++strip) {
        firstValue(file, Form("DTD%02dS%02d", det, strip), dt[det][strip]);
      }
    }
    if (oldDir) oldDir->cd();
    std::cout << "Loaded online calibration: " << expandEnv(path) << std::endl;
    return true;
  }

  void enableSelfAlignment()
  {
    loaded = false;
    selfAlign = true;
    std::cout << "Online self-alignment is enabled for strip DTD calibration." << std::endl;
  }

  bool usingSelfAlignment() const { return selfAlign && !loaded; }

  double signalVelocity(int det) const
  {
    (void)det;
    return kSignalVelocityCmPerNs;
  }

  void updateSelfAlignment(int det, int strip, double rawTimeDiffNs)
  {
    if (!usingSelfAlignment() || det < 0 || det >= 24 || strip < 0 || strip >= 32) return;
    if (!std::isfinite(rawTimeDiffNs)) return;
    std::vector<double>& samples = selfSamplesNs[det][strip];
    const double currentOffset = selfOffsetNs[det][strip] + commonOffsetNs;
    const double residual = rawTimeDiffNs - currentOffset;
    const double acceptWindowNs = 2. * kStripHalfLengthCm / kSignalVelocityCmPerNs;
    if (!samples.empty() && std::fabs(residual) > acceptWindowNs) return;
    if (samples.empty() && std::fabs(rawTimeDiffNs) > acceptWindowNs) return;
    samples.push_back(rawTimeDiffNs);
    if (samples.size() > 128) samples.erase(samples.begin());
    selfOffsetNs[det][strip] = robustCenter(samples) - commonOffsetNs;
    ++selfCount[det][strip];
  }

  void updateTrackResidualAlignment(int det, int strip, double residualCm)
  {
    if (!usingSelfAlignment() || det < 0 || det >= 24 || strip < 0 || strip >= 32) return;
    if (!std::isfinite(residualCm) || std::fabs(residualCm) > 8.) return;
    std::vector<double>& samples = trackResidualSamplesCm[det][strip];
    samples.push_back(residualCm);
    if (samples.size() > 96) samples.erase(samples.begin());
    if (samples.size() < 16 || (samples.size() % 8) != 0) return;

    const double residualCenter = robustCenter(samples);
    if (!std::isfinite(residualCenter) || std::fabs(residualCenter) < 0.25) return;
    const double deltaOffsetNs = 2. * residualCenter / kSignalVelocityCmPerNs;
    const double maxStepNs = 0.04;
    const double stepNs = std::max(-maxStepNs, std::min(maxStepNs, 0.20 * deltaOffsetNs));
    selfOffsetNs[det][strip] += stepNs;
    ++trackAlignCount[det][strip];
  }

  void addGlobalCenterSample(int det, double xLocalCm)
  {
    if (!usingSelfAlignment() || det < 0 || det >= 24) return;
    if (!std::isfinite(xLocalCm) || std::fabs(xLocalCm) > kStripHalfLengthCm) return;
    std::vector<double>& samples = centerSamplesCm[det];
    samples.push_back(xLocalCm);
    if (samples.size() > 2048) samples.erase(samples.begin());
  }

  void updateGlobalCenterAnchor()
  {
    if (!usingSelfAlignment()) return;

    std::vector<double> detectorCenters;
    detectorCenters.reserve(24);
    for (int det = 0; det < 24; ++det) {
      const std::vector<double>& samples = centerSamplesCm[det];
      if (samples.size() < 512) continue;
      const double left = quantile(samples, 0.05);
      const double right = quantile(samples, 0.95);
      const double width = right - left;
      if (!std::isfinite(width) || width < 14. || width > kStripLengthCm + 1.) continue;
      const double center = 0.5 * (left + right);
      if (std::isfinite(center) && std::fabs(center) < 8.) detectorCenters.push_back(center);
    }
    if (detectorCenters.size() < 2) return;

    const double commonCenter = robustCenter(detectorCenters);
    if (!std::isfinite(commonCenter) || std::fabs(commonCenter) < 0.15) return;

    const double maxStepCm = 0.12;
    const double stepCm = std::max(-maxStepCm, std::min(maxStepCm, 0.20 * commonCenter));
    commonOffsetNs += 2. * stepCm / kSignalVelocityCmPerNs;
    commonPositionCorrectionCm += stepCm;
    ++commonAlignCount;

    for (int det = 0; det < 24; ++det) centerSamplesCm[det].clear();
  }

  double selfDtPs(int det, int strip) const
  {
    if (!usingSelfAlignment() || det < 0 || det >= 24 || strip < 0 || strip >= 32) return 0.;
    const double stripOffset = selfCount[det][strip] > 0 ? selfOffsetNs[det][strip] : 0.;
    return 500. * (stripOffset + commonOffsetNs);
  }

  double correctedTimeDiff(int det, int strip, double rawTimeDiffNs) const
  {
    if (!usingSelfAlignment() || det < 0 || det >= 24 || strip < 0 || strip >= 32) return rawTimeDiffNs;
    return rawTimeDiffNs - selfOffsetNs[det][strip] - commonOffsetNs;
  }

  double correctedPositionTime(int det, int strip, int side, double time) const
  {
    if (det < 0 || det >= 24 || strip < 0 || strip >= 32) return time;
    if (!loaded && !usingSelfAlignment()) return time;

    const double dtPs = loaded ? dt[det][strip] : selfDtPs(det, strip);
    return time + (side == 0 ? -1.e-3 : 1.e-3) * dtPs;
  }
};

std::string resolveCalibrationFile(const std::string& requested)
{
  const std::string expanded = expandEnv(requested);
  if (!expanded.empty() && isFile(expanded)) return expanded;
  return expanded;
}

bool parseEventAt(std::ifstream& in, uint64_t eventAddress, EventHeader& event)
{
  in.clear();
  in.seekg(eventAddress, std::ios::beg);
  event = EventHeader{};
  event.head = readUIntLE(in, 2);
  event.length = readUIntLE(in, 4);
  event.triggerId = readUIntLE(in, 6);
  event.assembleNum = readUIntLE(in, 1);
  if (!in) return false;

  uint64_t assembleAddress = eventAddress + 13;
  for (uint64_t i = 0; i < event.assembleNum; ++i) {
    in.clear();
    in.seekg(assembleAddress, std::ios::beg);
    AssembleHeader assemble;
    assemble.id = readUIntLE(in, 2);
    assemble.length = readUIntLE(in, 4);
    assemble.feeNum = readUIntLE(in, 2);
    if (!in || assemble.length < 8) return false;

    uint64_t feeAddress = assembleAddress + 8;
    for (uint64_t j = 0; j < assemble.feeNum; ++j) {
      in.clear();
      in.seekg(feeAddress, std::ios::beg);
      FeeHeader fee;
      fee.id = readUIntLE(in, 2);
      fee.dataLength = readUIntLE(in, 3);
      fee.address = feeAddress;
      assemble.fees.push_back(fee);
      feeAddress += 5 + fee.dataLength;
    }
    event.assembles.push_back(assemble);
    assembleAddress += assemble.length;
  }
  return true;
}

bool readFileMenu(std::ifstream& in, std::vector<uint64_t>& eventAddresses)
{
  eventAddresses.clear();
  in.clear();
  in.seekg(0, std::ios::beg);
  const uint64_t eventNum = readUIntLE(in, 2);
  if (!in || eventNum == 0 || eventNum > 100000000ULL) return false;
  eventAddresses.reserve(eventNum);
  for (uint64_t i = 0; i < eventNum; ++i) {
    const uint64_t triggerId = readUIntLE(in, 6);
    (void)triggerId;
    const uint64_t eventAddress = readUIntLE(in, 8);
    if (!in) return false;
    eventAddresses.push_back(eventAddress);
  }
  return true;
}

std::vector<RawDigi> unpackITOFEvent(std::ifstream& in,
                                     const EventHeader& event,
                                     const std::set<uint64_t>& assembleIds,
                                     const std::unordered_map<int, EleMapEntry>& eleMap,
                                     const std::unordered_map<uint64_t, std::vector<double>>& clsb)
{
  std::vector<RawDigi> digis;

  for (const auto& assemble : event.assembles) {
    if (assembleIds.find(assemble.id) == assembleIds.end()) continue;
    for (const auto& fee : assemble.fees) {
      const int bdm = static_cast<int>(fee.id & 0x00ff);
      if (fee.dataLength < 7) continue;
      const int channelCount = static_cast<int>((fee.dataLength - 7) / 11);
      const uint64_t realDataAddress = fee.address + 5;
      in.clear();
      in.seekg(realDataAddress, std::ios::beg);

      for (int k = 0; k < channelCount; ++k) {
        const uint64_t triggerPrecise = readUIntLE(in, 1);
        const uint64_t trailingPrecise1 = readUIntLE(in, 1);
        const uint64_t trailingRough1 = readUIntLE(in, 2);
        const uint64_t leadingPrecise2 = readUIntLE(in, 1);
        const uint64_t leadingRough2 = readUIntLE(in, 2);
        const uint64_t leadingPrecise1 = readUIntLE(in, 1);
        const uint64_t leadingRough1 = readUIntLE(in, 2);
        const uint64_t channel = readUIntLE(in, 1);
        if (!in) break;

        const uint64_t keyFront = (assemble.id << 20) + (static_cast<uint64_t>(bdm) << 12) + (channel << 4);
        const double leadingCorr1 = clsbCorrection(clsb, keyFront, leadingPrecise1);
        const double leadingCorr2 = clsbCorrection(clsb, keyFront + 1, leadingPrecise2);
        const double trailingCorr1 = clsbCorrection(clsb, keyFront + 2, trailingPrecise1);

        const double leadingPs =
          ((256. - static_cast<double>(leadingRough1)) - leadingCorr1 +
           (256. - static_cast<double>(leadingRough2)) - leadingCorr2) /
          2. / 480. * 1000. * 1000.;
        const double trailingPs =
          ((256. - static_cast<double>(trailingRough1)) - trailingCorr1) /
          480. * 1000. * 1000.;
        double totPs = trailingPs - leadingPs;
        if (totPs < -1.3e8) totPs += 65536. / 480. * 1000. * 1000.;
        const double leading = leadingPs * 1.e-3;
        const double tot = totPs * 1.e-3;
        if (tot <= 0.) continue;
        const double leadingRelNs = leading - 256. / 480. * 1000.;
        const double doubleChainDiffPs =
          ((-static_cast<double>(leadingRough1) - leadingCorr1) -
           (-static_cast<double>(leadingRough2) - leadingCorr2)) /
          480. * 1000. * 1000.;
        const int eleAddress = bdm * 100 + static_cast<int>(channel);
        const auto mapIt = eleMap.find(eleAddress);
        if (mapIt == eleMap.end()) continue;
        digis.push_back(RawDigi(mapIt->second.det,
                                mapIt->second.strip,
                                mapIt->second.side,
                                bdm,
                                static_cast<int>(channel),
                                static_cast<int>(event.triggerId),
                                leading,
                                tot,
                                leadingRelNs,
                                doubleChainDiffPs));
      }
    }
  }
  return digis;
}

std::vector<OnlineHit> makeHits(const std::vector<RawDigi>& digis,
                                const NomagGeometry& geo,
                                CalibrationTable& calibration,
                                double totMin,
                                double totMax)
{
  std::map<std::pair<int, int>, std::vector<const RawDigi*>> byStrip;
  for (const auto& digi : digis) {
    if (digi.det < 0 || digi.det >= geo.nDet()) continue;
    if (digi.strip < 0 || digi.strip >= geo.nStrips(digi.det)) continue;
    if (digi.side < 0 || digi.side > 1) continue;
    if (digi.tot < totMin || digi.tot > totMax) continue;
    byStrip[{digi.det, digi.strip}].push_back(&digi);
  }

  std::vector<OnlineHit> hits;
  for (const auto& item : byStrip) {
    std::vector<const RawDigi*> side0Digis;
    std::vector<const RawDigi*> side1Digis;
    for (const RawDigi* digi : item.second) {
      if (digi->side == 0) side0Digis.push_back(digi);
      if (digi->side == 1) side1Digis.push_back(digi);
    }
    if (side0Digis.empty() || side1Digis.empty()) continue;

    const int det = item.first.first;
    const int strip = item.first.second;
    const RawDigi* side0 = nullptr;
    const RawDigi* side1 = nullptr;
    double bestTot = -1.;
    double bestDistance = 1.e99;
    bool foundPhysicalPair = false;
    const double maxLocalX = kStripHalfLengthCm;

    for (const RawDigi* candidate0 : side0Digis) {
      for (const RawDigi* candidate1 : side1Digis) {
        const double rawDiff = candidate0->time - candidate1->time;
        const double correctedDiff = calibration.correctedTimeDiff(det, strip, rawDiff);
        const double xLocal = correctedDiff / 2. * calibration.signalVelocity(det);
        const double distance = std::fabs(xLocal);
        const double pairTot = candidate0->tot + candidate1->tot;
        const bool physicalPair = distance <= maxLocalX;
        if (physicalPair) {
          if (!foundPhysicalPair || pairTot > bestTot) {
            side0 = candidate0;
            side1 = candidate1;
            bestTot = pairTot;
            bestDistance = distance;
            foundPhysicalPair = true;
          }
        } else if (!foundPhysicalPair && distance < bestDistance) {
          side0 = candidate0;
          side1 = candidate1;
          bestTot = pairTot;
          bestDistance = distance;
        }
      }
    }

    if (!side0 || !side1 || !foundPhysicalPair) continue;
    const double hitTot = (side0->tot + side1->tot) / 2.;
    const double correctedTime0 = calibration.correctedPositionTime(det, strip, 0, side0->time);
    const double correctedTime1 = calibration.correctedPositionTime(det, strip, 1, side1->time);
    const double xLocal = (correctedTime0 - correctedTime1) / 2. * calibration.signalVelocity(det);
    const TVector3 pos = geo.hitPosition(det, strip, xLocal);
    calibration.updateSelfAlignment(det, strip, side0->time - side1->time);
    hits.push_back(OnlineHit(det,
                             strip,
                             side0->triggerId,
                             pos.X(),
                             pos.Y(),
                             pos.Z(),
                             0.,
                             hitTot,
                             side0->time - side1->time));
  }
  return hits;
}

class ITOFRootOnlineMonitor {
public:
  enum { kEventHistoryCapacity = 200 };

  ITOFRootOnlineMonitor(const std::string& inputPath,
                        int httpPort,
                        Long64_t eventLimit,
                        int refreshMs,
                        bool pollInput,
                        const std::string& calibPath)
    : input(inputPath),
      calibFile(calibPath),
      httpPortValue(httpPort),
      maxEvents(eventLimit),
      refreshMillis(refreshMs),
      poll(pollInput),
      server(new THttpServer(Form("http:%d", httpPort))),
      wallPosZ(new TH2D("hPos_z", "Wall hit map (Z);X [cm];Y [cm]", 160, -80, 80, 120, -80, 40)),
      wallPosXn(new TH2D("hPos_xn", "Wall hit map (X-);Z [cm];Y [cm]", 120, -60, 60, 120, -80, 40)),
      wallPosXp(new TH2D("hPos_xp", "Wall hit map (X+);Z [cm];Y [cm]", 120, -60, 60, 120, -80, 40)),
      hit3D(new TH3D("hHit", "3D hits;X [cm];Y [cm];Z [cm]", 200, -100, 100, 120, -80, 40, 160, -40, 120)),
      digiMap(new TH2D("DigiMap", "Digi hit map;Detector ID;Strip ID", 24, -0.5, 23.5, 32, -0.5, 31.5)),
      hitMultiplicity(new TH1D("HitMultiplicity", "Hit multiplicity;Hits / event;Events", 80, -0.5, 79.5)),
      digiMultiplicity(new TH1D("DigiMultiplicity", "Digi multiplicity;Digis / event;Events", 100, -0.5, 199.5)),
      totAll(new TH1D("TOT_all", "TOT spectrum;TOT [ps];Counts", 160, 0, 40000)),
      latestHits3D(new TPolyMarker3D(0)),
      latestTrack3D(new TPolyMarker3D(0)),
      eventHistoryStatus(new TText(0.02, 0.5, "capacity=200 latest=0 first=0 count=0 slot=-1")),
      channelHealthStatus(new TText(0.02, 0.5, "{\"level\":\"waiting\",\"warnings\":[]}")),
      status(new TText(0.02, 0.5, "not started"))
  {
    const char* vmc = std::getenv("VMCWORKDIR");
    const std::string root = vmc ? vmc : gSystem->WorkingDirectory();
    const char* mapEnv = std::getenv("ITOF_ONLINE_MAP");
    const char* clsbEnv = std::getenv("ITOF_ONLINE_CLSB");
    const char* trackAlgoEnv = std::getenv("ITOF_ONLINE_TRACK_ALGO");
    trackAlgorithm = (trackAlgoEnv && std::string(trackAlgoEnv) == "RANSAC") ? "RANSAC" : "Fast";
    std::cout << "Event display track algorithm: " << trackAlgorithm << std::endl;
    eleMap = loadEleMap(mapEnv ? mapEnv : root + "/itof/reco/map/iTOFMap_newchip2511.csv");
    collectEleMapAxes();
    clsb = loadCLSB(clsbEnv ? clsbEnv : root + "/itof/macro/newchip/CLSB.txt");
    const std::string resolvedCalibFile = resolveCalibrationFile(calibFile);
    if (resolvedCalibFile.empty() || !isFile(resolvedCalibFile) || !calibration.load(resolvedCalibFile)) {
      if (!resolvedCalibFile.empty()) {
        std::cerr << "Calibration file is not available: " << resolvedCalibFile << std::endl;
      }
      calibration.enableSelfAlignment();
    }
    const std::vector<uint64_t> ids = defaultAssembleIds();
    assembleIds.insert(ids.begin(), ids.end());

    detectorPos.reserve(mappedDetectors.size());
    for (int det : mappedDetectors) {
      if (det < 0 || det >= geometry.nDet()) continue;
      detectorPosIndex[det] = detectorPos.size();
      detectorPos.push_back(std::unique_ptr<TH2D>(
        new TH2D(Form("hPos_%d", det),
                 Form("Detector %d hit map;X [cm];Y [cm]", det),
                 100,
                 -25.,
                 25.,
                 40,
                 -20.,
                 20.)));
    }
    loadEventDisplayGeometry(root);

    for (int fee : mappedFees) {
      const std::set<int>& channels = mappedChannelsByFee[fee];
      const int firstChannel = channels.empty() ? 0 : *channels.begin();
      const int lastChannel = channels.empty() ? 15 : *channels.rbegin();
      const int channelBins = std::max(1, lastChannel - firstChannel + 1);
      feeHistIndex[fee] = countChannel.size();
      countChannel.push_back(std::unique_ptr<TH1D>(
        new TH1D(Form("count_channel_%d", fee),
                 Form("Channel occupancy - FEE %d;Channel;Counts", fee),
                 channelBins, firstChannel - 0.5, lastChannel + 0.5)));
      totFee.push_back(std::unique_ptr<TH2D>(
        new TH2D(Form("count_FEE_%d", fee),
                 Form("TOT map - FEE %d;Channel;TOT [ps]", fee),
                 channelBins, firstChannel - 0.5, lastChannel + 0.5, 100, 0, 40000)));
      for (int channel : channels) {
        channelHistIndex[std::make_pair(fee, channel)] = totChannel.size();
        totChannel.push_back(std::unique_ptr<TH1D>(
          new TH1D(Form("TOT_channel_%d_%d", fee, channel),
                   Form("TOT spectrum - FEE %d CH %d;TOT [ps];Counts", fee, channel),
                   100, -10000, 40000)));
        leadingChannel.push_back(std::unique_ptr<TH1D>(
          new TH1D(Form("Tt_Leading_channel_%d_%d", fee, channel),
                   Form("Leading time - FEE %d CH %d;Leading - trigger [ns];Counts", fee, channel),
                   300, -400, -100)));
        doubleChainTimeDiff.push_back(std::unique_ptr<TH1D>(
          new TH1D(Form("double_chain_timediff_%d_%d", fee, channel),
                   Form("Double-chain time diff - FEE %d CH %d;#Delta chain [ps];Counts", fee, channel),
                   50, -100, 100)));
      }
    }
    for (int det : mappedDetectors) {
      if (det < 0 || det >= geometry.nDet()) continue;
      const std::set<int>& sides = mappedSidesByDetector[det];
      for (int side : sides) {
        detectorSideHistIndex[std::make_pair(det, side)] = totDet.size();
        totDet.push_back(std::unique_ptr<TH2D>(
          new TH2D(Form("TOT_Det_%d_%d", det, side),
                   Form("Detector %d side %d TOT map;Strip;TOT [ps]", det, side),
                   34, -1, 33, 100, 0, 40000)));
      }
    }
    for (int det : mappedDetectors) {
      if (det < 0 || det >= geometry.nDet()) continue;
      leadingTimeIndex[det] = leadingTime.size();
      leadingTime.push_back(std::unique_ptr<TH1D>(
        new TH1D(Form("LeadingTime_det%d", det),
                 Form("Leading time - detector %d;Leading - trigger [ps];Counts", det),
                 120, -500000, 0)));
    }

    configureOnlineRootStyle();
    configureDigiMapAxes();
    styleOnlineHist2D(wallPosZ.get());
    styleOnlineHist2D(wallPosXn.get());
    styleOnlineHist2D(wallPosXp.get());
    styleOnlineHist2D(digiMap.get());
    styleOnlineHist1D(hitMultiplicity.get());
    styleOnlineHist1D(digiMultiplicity.get());
    styleOnlineHist1D(totAll.get());
    for (auto& hist : detectorPos) styleOnlineHist2D(hist.get());
    for (auto& hist : countChannel) styleOnlineHist1D(hist.get());
    for (auto& hist : totFee) styleOnlineHist2D(hist.get());
    for (auto& hist : totChannel) styleOnlineHist1D(hist.get());
    for (auto& hist : leadingChannel) styleOnlineHist1D(hist.get());
    for (auto& hist : doubleChainTimeDiff) styleOnlineHist1D(hist.get());
    for (auto& hist : totDet) styleOnlineHist2D(hist.get());
    for (auto& hist : leadingTime) styleOnlineHist1D(hist.get());

    latestHits3D->SetName("LatestHits3D");
    latestHits3D->SetMarkerStyle(20);
    latestHits3D->SetMarkerColor(kRed + 1);
    latestHits3D->SetMarkerSize(3.0);

    latestTrack3D->SetName("LatestTrack3D");
    latestTrack3D->SetMarkerStyle(20);
    latestTrack3D->SetMarkerColor(kGreen + 2);
    latestTrack3D->SetMarkerSize(1.0);

    eventHistoryHits.reserve(kEventHistoryCapacity);
    eventHistoryTracks.reserve(kEventHistoryCapacity);
    for (int i = 0; i < kEventHistoryCapacity; ++i) {
      eventHistoryHits.push_back(std::unique_ptr<TPolyMarker3D>(new TPolyMarker3D(0)));
      eventHistoryHits.back()->SetName(Form("EventHits_%03d", i));
      eventHistoryHits.back()->SetUniqueID(0);
      eventHistoryHits.back()->SetMarkerStyle(20);
      eventHistoryHits.back()->SetMarkerColor(kRed + 1);
      eventHistoryHits.back()->SetMarkerSize(3.0);

      eventHistoryTracks.push_back(std::unique_ptr<TPolyMarker3D>(new TPolyMarker3D(0)));
      eventHistoryTracks.back()->SetName(Form("EventTrack_%03d", i));
      eventHistoryTracks.back()->SetUniqueID(0);
      eventHistoryTracks.back()->SetMarkerStyle(20);
      eventHistoryTracks.back()->SetMarkerColor(kGreen + 2);
      eventHistoryTracks.back()->SetMarkerSize(1.0);
    }
    eventHistoryStatus->SetName("EventHistoryStatus");
    eventHistoryStatus->SetTitle("capacity=200 latest=0 first=0 count=0 slot=-1");

    channelHealthStatus->SetName("ChannelHealth");
    channelHealthStatus->SetTitle("{\"level\":\"waiting\",\"ready_detectors\":0,\"warnings\":[]}");
    channelHealthStatus->SetText(0.02, 0.5, channelHealthStatus->GetTitle());

    status->SetName("Status");
    status->SetTextSize(0.04);

    server->Register("/iTOF/Position", wallPosZ.get());
    server->Register("/iTOF/Position", wallPosXn.get());
    server->Register("/iTOF/Position", wallPosXp.get());
    server->Register("/iTOF/Position", hit3D.get());
    if (eventGeoManager) {
      server->Register("/iTOF/EventDisplay", eventGeoManager);
    }
    if (eventTopVolume) {
      server->Register("/iTOF/EventDisplay", eventTopVolume);
    }
    for (TGeoVolume* volume : eventDisplayVolumes) {
      server->Register("/iTOF/EventDisplay", volume);
    }
    for (auto& hist : detectorPos) {
      server->Register("/iTOF/Position/Detectors", hist.get());
    }
    server->Register("/iTOF/Debug", digiMap.get());
    server->Register("/iTOF", hitMultiplicity.get());
    server->Register("/iTOF", digiMultiplicity.get());
    server->Register("/iTOF", latestHits3D.get());
    server->Register("/iTOF", latestTrack3D.get());
    for (auto& marker : eventHistoryHits) server->Register("/iTOF/EventHistory", marker.get());
    for (auto& marker : eventHistoryTracks) server->Register("/iTOF/EventHistory", marker.get());
    server->Register("/iTOF/EventHistory", eventHistoryStatus.get());
    server->Register("/iTOF/Warnings", channelHealthStatus.get());
    server->Register("/iTOF", status.get());
    server->Register("/iTOF/Unpacker/Overview", totAll.get());
    for (auto& hist : countChannel) server->Register("/iTOF/Unpacker/FEE", hist.get());
    for (auto& hist : totFee) server->Register("/iTOF/Unpacker/FEE", hist.get());
    for (auto& hist : totChannel) server->Register("/iTOF/Unpacker/ChannelTOT", hist.get());
    for (auto& hist : leadingChannel) server->Register("/iTOF/Unpacker/ChannelLeading", hist.get());
    for (auto& hist : doubleChainTimeDiff) server->Register("/iTOF/Unpacker/ChannelTiming", hist.get());
    for (auto& hist : totDet) server->Register("/iTOF/Unpacker/Detector", hist.get());
    for (auto& hist : leadingTime) server->Register("/iTOF/Unpacker/Timing", hist.get());
    server->SetItemField("/iTOF/LatestHits3D", "_drawitem", "p");
    server->SetItemField("/iTOF/LatestTrack3D", "_drawitem", "p");
    for (int i = 0; i < kEventHistoryCapacity; ++i) {
      server->SetItemField(Form("/iTOF/EventHistory/EventHits_%03d", i), "_drawitem", "p");
      server->SetItemField(Form("/iTOF/EventHistory/EventTrack_%03d", i), "_drawitem", "p");
    }
    server->SetItemField("/iTOF/Position/hPos_z", "_drawitem", "colz");
    server->SetItemField("/iTOF/Position/hPos_xn", "_drawitem", "colz");
    server->SetItemField("/iTOF/Position/hPos_xp", "_drawitem", "colz");
    server->SetItemField("/iTOF/Position/hHit", "_drawitem", "box");
    if (eventGeoManager) {
      server->SetItemField(Form("/iTOF/EventDisplay/%s", eventGeoManager->GetName()), "_drawitem", "ogl");
    }
    if (eventTopVolume) {
      server->SetItemField("/iTOF/EventDisplay/TOPiTOF", "_drawitem", "ogl");
    }
    for (TGeoVolume* volume : eventDisplayVolumes) {
      server->SetItemField(Form("/iTOF/EventDisplay/%s", volume->GetName()), "_drawitem", "ogl");
    }
    for (int det : mappedDetectors) {
      if (det < 0 || det >= geometry.nDet()) continue;
      server->SetItemField(Form("/iTOF/Position/Detectors/hPos_%d", det), "_drawitem", "colz");
    }
    server->SetItemField("/iTOF/Debug/DigiMap", "_drawitem", "colz");
    server->SetItemField("/iTOF/Unpacker/Overview/TOT_all", "_drawitem", "hist");
    for (int fee : mappedFees) {
      server->SetItemField(Form("/iTOF/Unpacker/FEE/count_FEE_%d", fee), "_drawitem", "colz");
      server->SetItemField(Form("/iTOF/Unpacker/FEE/count_channel_%d", fee), "_drawitem", "hist");
      for (int channel : mappedChannelsByFee[fee]) {
        server->SetItemField(Form("/iTOF/Unpacker/ChannelTOT/TOT_channel_%d_%d", fee, channel), "_drawitem", "hist");
        server->SetItemField(Form("/iTOF/Unpacker/ChannelLeading/Tt_Leading_channel_%d_%d", fee, channel), "_drawitem", "hist");
        server->SetItemField(Form("/iTOF/Unpacker/ChannelTiming/double_chain_timediff_%d_%d", fee, channel), "_drawitem", "hist");
      }
    }
    for (int det : mappedDetectors) {
      if (det < 0 || det >= geometry.nDet()) continue;
      for (int side : mappedSidesByDetector[det]) {
        server->SetItemField(Form("/iTOF/Unpacker/Detector/TOT_Det_%d_%d", det, side), "_drawitem", "colz");
      }
    }
    for (int det : mappedDetectors) {
      if (det < 0 || det >= geometry.nDet()) continue;
      server->SetItemField(Form("/iTOF/Unpacker/Timing/LeadingTime_det%d", det), "_drawitem", "hist");
    }
  }

  void collectEleMapAxes()
  {
    mappedDetectors.clear();
    mappedStrips.clear();
    mappedSides.clear();
    mappedFees.clear();
    mappedChannels.clear();
    mappedChannelsByFee.clear();
    mappedSidesByDetector.clear();
    mappedStripsByDetector.clear();
    for (const auto& item : eleMap) {
      const int fee = item.first / 100;
      const int channel = item.first % 100;
      const EleMapEntry& entry = item.second;
      mappedDetectors.insert(entry.det);
      mappedStrips.insert(entry.strip);
      mappedSides.insert(entry.side);
      mappedFees.insert(fee);
      mappedChannels.insert(channel);
      mappedChannelsByFee[fee].insert(channel);
      mappedSidesByDetector[entry.det].insert(entry.side);
      mappedStripsByDetector[entry.det].insert(entry.strip);
    }
    if (mappedDetectors.empty()) {
      for (int det = 0; det < 4; ++det) mappedDetectors.insert(det);
    }
    if (mappedStrips.empty()) {
      for (int strip = 0; strip < 32; ++strip) mappedStrips.insert(strip);
    }
    if (mappedSides.empty()) {
      mappedSides.insert(0);
      mappedSides.insert(1);
    }
    if (mappedFees.empty()) {
      for (int fee = 0; fee < 16; ++fee) mappedFees.insert(fee);
    }
    if (mappedChannels.empty()) {
      for (int channel = 0; channel < 16; ++channel) mappedChannels.insert(channel);
    }
    for (int fee : mappedFees) {
      if (mappedChannelsByFee[fee].empty()) mappedChannelsByFee[fee] = mappedChannels;
    }
    for (int det : mappedDetectors) {
      if (mappedSidesByDetector[det].empty()) mappedSidesByDetector[det] = mappedSides;
      if (mappedStripsByDetector[det].empty()) mappedStripsByDetector[det] = mappedStrips;
    }
  }

  void configureDigiMapAxes()
  {
    if (!digiMap || mappedDetectors.empty() || mappedStrips.empty()) return;
    const int firstDet = *mappedDetectors.begin();
    const int lastDet = *mappedDetectors.rbegin();
    const int firstStrip = *mappedStrips.begin();
    const int lastStrip = *mappedStrips.rbegin();
    digiMap->SetBins(std::max(1, lastDet - firstDet + 1),
                     firstDet - 0.5,
                     lastDet + 0.5,
                     std::max(1, lastStrip - firstStrip + 1),
                     firstStrip - 0.5,
                     lastStrip + 0.5);
  }

  void run()
  {
    if (eleMap.empty() || clsb.empty()) {
      std::cerr << "Cannot start monitor because electronics map or CLSB table is empty." << std::endl;
      return;
    }

    std::cout << "JSROOT URL: http://localhost:" << httpPortValue << std::endl;
    do {
      processAvailableFiles();
      if (!poll) break;
      serviceHttpDuringWait(refreshMillis);
    } while (true);

    while (!gROOT->IsBatch()) {
      gSystem->ProcessEvents();
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    std::cout << "Processed events: " << processedEvents << std::endl;
  }

private:
  void serviceHttpDuringWait(int waitMs)
  {
    const int sliceMs = 50;
    int waited = 0;
    while (waited < waitMs) {
      gSystem->ProcessEvents();
      const int sleepMs = std::min(sliceMs, waitMs - waited);
      std::this_thread::sleep_for(std::chrono::milliseconds(sleepMs));
      waited += sleepMs;
    }
    gSystem->ProcessEvents();
  }

  int eventDisplayLayerIndex(int det) const
  {
    int index = 0;
    for (int mappedDet : mappedDetectors) {
      if (mappedDet == det) return index;
      ++index;
    }
    return det >= 0 ? det : 0;
  }

  int eventDisplayDetectorFromZ(double z) const
  {
    if (mappedDetectors.empty()) return static_cast<int>(std::floor(z / kEventDisplayLayerSpacingCm + 0.5));
    int index = static_cast<int>(std::floor(z / kEventDisplayLayerSpacingCm + 0.5));
    index = std::max(0, std::min(index, static_cast<int>(mappedDetectors.size()) - 1));
    auto iter = mappedDetectors.begin();
    std::advance(iter, index);
    return *iter;
  }

  double readoutLengthCm(TGeoVolume* volume) const
  {
    if (!volume) return 0.;
    double length = 0.;
    for (int i = 0; i < volume->GetNdaughters(); ++i) {
      TGeoNode* node = volume->GetNode(i);
      TGeoVolume* child = node ? node->GetVolume() : nullptr;
      if (!child || std::string(child->GetName()) != "readout") continue;
      TGeoBBox* box = dynamic_cast<TGeoBBox*>(child->GetShape());
      if (box) length = std::max(length, 2. * box->GetDX());
    }
    return length;
  }

  TGeoVolume* bestMrpcVolume(TGeoManager* manager) const
  {
    if (!manager) return nullptr;
    TObjArray* volumes = manager->GetListOfVolumes();
    TGeoVolume* best = nullptr;
    double bestScore = 1.e300;
    for (Int_t i = 0; volumes && i < volumes->GetEntries(); ++i) {
      TGeoVolume* volume = dynamic_cast<TGeoVolume*>(volumes->At(i));
      if (!volume || std::string(volume->GetName()) != "MRPC") continue;
      const double length = readoutLengthCm(volume);
      const double score = std::fabs(length - kStripLengthCm) + 0.01 * std::fabs(volume->GetNdaughters() - 109);
      if (length > 0. && score < bestScore) {
        best = volume;
        bestScore = score;
      }
    }
    return best;
  }

  bool createExtractedEventDisplayGeometry(const std::string& projectRoot)
  {
    const int nLayers = std::max(1, static_cast<int>(mappedDetectors.size()));
    const std::string geoPath = projectRoot + "/itof/geo/itof_geomanager_vbatch2409.root";
    if (!isFile(geoPath)) return false;

    eventGeoFile = TFile::Open(geoPath.c_str(), "READ");
    if (!eventGeoFile || eventGeoFile->IsZombie()) {
      eventGeoFile = nullptr;
      return false;
    }
    eventGeoManager = dynamic_cast<TGeoManager*>(eventGeoFile->Get("FAIRGeom"));
    TGeoVolume* sourceMrpc = bestMrpcVolume(eventGeoManager);
    if (!eventGeoManager || !sourceMrpc) {
      if (eventGeoFile) eventGeoFile->Close();
      eventGeoFile = nullptr;
      eventGeoManager = nullptr;
      return false;
    }

    TGeoVolume* top = new TGeoVolumeAssembly("MapTOPiTOF");
    TGeoVolume* detectorUnit = new TGeoVolumeAssembly("iTOFDetectorUnit");
    detectorUnit->AddNode(sourceMrpc, 1, new TGeoTranslation(0., 0., 0.));

    int layer = 0;
    for (int det : mappedDetectors) {
      const double z = layer * kEventDisplayLayerSpacingCm;
      top->AddNode(detectorUnit, det + 1, new TGeoTranslation(0., 0., z));
      ++layer;
    }

    eventGeoManager->SetTopVolume(top);
    eventGeoManager->SetVisOption(1);
    eventGeoManager->SetVisLevel(7);
    eventTopVolume = top;
    eventDisplayVolumes.clear();
    eventDisplayVolumes.push_back(detectorUnit);
    eventDisplayVolumes.push_back(sourceMrpc);
    std::cout << "Created map event display geometry from " << geoPath
              << " using MRPC readout length " << readoutLengthCm(sourceMrpc)
              << " cm, " << nLayers << " detector layer(s), spacing "
              << kEventDisplayLayerSpacingCm << " cm." << std::endl;
    return true;
  }

  void createMapEventDisplayGeometry(const std::string& projectRoot)
  {
    if (createExtractedEventDisplayGeometry(projectRoot)) return;

    const int nLayers = std::max(1, static_cast<int>(mappedDetectors.size()));
    int firstStrip = mappedStrips.empty() ? 0 : *mappedStrips.begin();
    int lastStrip = mappedStrips.empty() ? 31 : *mappedStrips.rbegin();
    for (int det : mappedDetectors) {
      const auto stripsIt = mappedStripsByDetector.find(det);
      if (stripsIt != mappedStripsByDetector.end()) {
        if (!stripsIt->second.empty()) {
          firstStrip = std::min(firstStrip, *stripsIt->second.begin());
          lastStrip = std::max(lastStrip, *stripsIt->second.rbegin());
        }
      }
    }
    const int stripSpan = std::max(1, lastStrip - firstStrip + 1);

    eventGeoManager = new TGeoManager("MapGeometry", "iTOF event display geometry generated from electronics map");
    TGeoMaterial* vacuum = new TGeoMaterial("MapVacuum", 0., 0., 0.);
    TGeoMaterial* glass = new TGeoMaterial("MapGlass", 28.1, 14., 2.4);
    TGeoMaterial* gas = new TGeoMaterial("MapGasGap", 39.9, 18., 0.0018);
    TGeoMaterial* pcb = new TGeoMaterial("MapReadoutPCB", 12., 6., 1.85);
    TGeoMaterial* stripMat = new TGeoMaterial("MapCopperStrip", 63.5, 29., 8.96);
    TGeoMaterial* frameMat = new TGeoMaterial("MapFrame", 26.98, 13., 2.7);
    TGeoMedium* vacuumMed = new TGeoMedium("MapVacuum", 1, vacuum);
    TGeoMedium* glassMed = new TGeoMedium("MapGlass", 2, glass);
    TGeoMedium* gasMed = new TGeoMedium("MapGasGap", 3, gas);
    TGeoMedium* pcbMed = new TGeoMedium("MapReadoutPCB", 4, pcb);
    TGeoMedium* stripMed = new TGeoMedium("MapCopperStrip", 5, stripMat);
    TGeoMedium* frameMed = new TGeoMedium("MapFrame", 6, frameMat);

    const double planeHalfX = kStripHalfLengthCm;
    const double pitchCm = 1.0;
    const double planeHalfY = std::max(16.0, stripSpan * pitchCm * 0.5);
    const double topHalfZ = (nLayers - 1) * kEventDisplayLayerSpacingCm + 3.0;
    TGeoVolume* top = eventGeoManager->MakeBox("TOPiTOF", vacuumMed,
                                               planeHalfX + 4.0,
                                               planeHalfY + 4.0,
                                               topHalfZ + 4.0);
    top->SetVisibility(false);
    eventGeoManager->SetTopVolume(top);

    TGeoVolume* detectorUnit = eventGeoManager->MakeBox("iTOFDetectorUnit", vacuumMed,
                                                        planeHalfX + 2.6,
                                                        planeHalfY + 1.6,
                                                        1.20);
    detectorUnit->SetVisibility(false);

    TGeoVolume* gasGap = eventGeoManager->MakeBox("GasGap", gasMed, planeHalfX, planeHalfY, 0.18);
    gasGap->SetLineColor(kCyan + 2);
    gasGap->SetFillColor(kCyan - 10);
    gasGap->SetTransparency(82);
    detectorUnit->AddNode(gasGap, 1, new TGeoTranslation(0., 0., 0.));

    TGeoVolume* glassPlate = eventGeoManager->MakeBox("GlassPlate", glassMed, planeHalfX + 0.35, planeHalfY + 0.35, 0.10);
    glassPlate->SetLineColor(kViolet + 1);
    glassPlate->SetFillColor(kViolet - 9);
    glassPlate->SetTransparency(38);
    detectorUnit->AddNode(glassPlate, 1, new TGeoTranslation(0., 0., -0.34));
    detectorUnit->AddNode(glassPlate, 2, new TGeoTranslation(0., 0., 0.34));

    TGeoVolume* readoutBoard = eventGeoManager->MakeBox("ReadoutBoard", pcbMed, planeHalfX + 0.85, planeHalfY + 0.65, 0.055);
    readoutBoard->SetLineColor(kBlue + 2);
    readoutBoard->SetFillColor(kAzure - 8);
    readoutBoard->SetTransparency(58);
    detectorUnit->AddNode(readoutBoard, 1, new TGeoTranslation(0., 0., -0.58));
    detectorUnit->AddNode(readoutBoard, 2, new TGeoTranslation(0., 0., 0.58));

    TGeoVolume* stripVol = eventGeoManager->MakeBox("ReadoutStrip", stripMed, planeHalfX + 0.55, 0.030, 0.035);
    stripVol->SetLineColor(kGray + 2);
    stripVol->SetFillColor(kGray + 1);
    stripVol->SetTransparency(5);
    for (int strip = firstStrip; strip <= lastStrip; ++strip) {
      const double y = (strip - (firstStrip + lastStrip) / 2.0) * pitchCm;
      detectorUnit->AddNode(stripVol, strip - firstStrip + 1, new TGeoTranslation(0., y, 0.69));
      detectorUnit->AddNode(stripVol, strip - firstStrip + 1 + stripSpan, new TGeoTranslation(0., y, -0.69));
    }

    TGeoVolume* longFrame = eventGeoManager->MakeBox("LongFrame", frameMed, planeHalfX + 1.25, 0.12, 0.78);
    longFrame->SetLineColor(kGray + 3);
    longFrame->SetFillColor(kGray + 2);
    longFrame->SetTransparency(12);
    detectorUnit->AddNode(longFrame, 1, new TGeoTranslation(0., planeHalfY + 0.72, 0.));
    detectorUnit->AddNode(longFrame, 2, new TGeoTranslation(0., -planeHalfY - 0.72, 0.));

    TGeoVolume* endFrame = eventGeoManager->MakeBox("EndFrame", frameMed, 0.16, planeHalfY + 0.85, 0.78);
    endFrame->SetLineColor(kGray + 3);
    endFrame->SetFillColor(kGray + 2);
    endFrame->SetTransparency(12);
    detectorUnit->AddNode(endFrame, 1, new TGeoTranslation(planeHalfX + 1.35, 0., 0.));
    detectorUnit->AddNode(endFrame, 2, new TGeoTranslation(-planeHalfX - 1.35, 0., 0.));

    TGeoVolume* sideConnector = eventGeoManager->MakeBox("SideConnector", pcbMed, 0.38, planeHalfY * 0.55, 0.25);
    sideConnector->SetLineColor(kOrange + 7);
    sideConnector->SetFillColor(kOrange - 3);
    sideConnector->SetTransparency(25);
    detectorUnit->AddNode(sideConnector, 1, new TGeoTranslation(planeHalfX + 1.75, 0., 0.72));
    detectorUnit->AddNode(sideConnector, 2, new TGeoTranslation(-planeHalfX - 1.75, 0., -0.72));

    int layer = 0;
    for (int det : mappedDetectors) {
      const double z = layer * kEventDisplayLayerSpacingCm;
      top->AddNode(detectorUnit, det + 1, new TGeoTranslation(0., 0., z));
      ++layer;
    }

    eventGeoManager->CloseGeometry();
    eventGeoManager->SetVisOption(1);
    eventGeoManager->SetVisLevel(4);
    eventTopVolume = top;
    eventDisplayVolumes.clear();
    eventDisplayVolumes.push_back(detectorUnit);
    std::cout << "Created map event display geometry with " << nLayers
              << " detector layer(s), " << stripSpan << " strip(s) per reusable unit, spacing "
              << kEventDisplayLayerSpacingCm << " cm." << std::endl;
  }

  void loadEventDisplayGeometry(const std::string& projectRoot)
  {
    createMapEventDisplayGeometry(projectRoot);

    const char* geoEnv = std::getenv("ITOF_ONLINE_GEO");
    if (!geoEnv || !*geoEnv) return;

    std::string geoPath = geoEnv;
    if (!isFile(geoPath)) {
      std::cerr << "Event display geometry file is not available." << std::endl;
      return;
    }

    eventGeoFile = TFile::Open(geoPath.c_str(), "READ");
    if (!eventGeoFile || eventGeoFile->IsZombie()) {
      std::cerr << "Cannot open event display geometry file: " << geoPath << std::endl;
      eventGeoFile = nullptr;
      return;
    }

    eventGeoManager = dynamic_cast<TGeoManager*>(eventGeoFile->Get("FAIRGeom"));
    if (!eventGeoManager) {
      std::cerr << "Cannot find FAIRGeom in event display geometry file: " << geoPath << std::endl;
      return;
    }

    eventGeoManager->SetVisOption(1);
    eventGeoManager->SetVisLevel(7);
    TObjArray* volumes = eventGeoManager->GetListOfVolumes();
    if (volumes) {
      for (Int_t i = 0; i < volumes->GetEntries(); ++i) {
        TGeoVolume* volume = dynamic_cast<TGeoVolume*>(volumes->At(i));
        if (volume) volume->SetTransparency(80);
      }
    }
    eventTopVolume = eventGeoManager->GetTopVolume();
    eventDisplayVolumes.clear();
    const char* selectableVolumes[] = {"itof", "M1", "MRPC"};
    for (const char* name : selectableVolumes) {
      TGeoVolume* volume = eventGeoManager->GetVolume(name);
      if (volume) eventDisplayVolumes.push_back(volume);
    }
    std::cout << "Loaded event display geometry: " << geoPath << std::endl;
  }

  void processAvailableFiles()
  {
    const std::vector<std::string> files = resolveInputFiles(input);
    for (const auto& file : files) {
      processFile(file);
      if (maxEvents >= 0 && processedEvents >= maxEvents) return;
    }
  }

  void processFile(const std::string& file)
  {
    std::ifstream in(file, std::ios::binary);
    if (!in) {
      std::cerr << "Cannot open raw file: " << file << std::endl;
      return;
    }

    std::vector<uint64_t> eventAddresses;
    if (!readFileMenu(in, eventAddresses)) {
      if (!poll) std::cerr << "Cannot read DAQ event menu: " << file << std::endl;
      return;
    }

    const Long64_t currentSize = fileSize(file);
    size_t& nextEvent = nextEventIndex[file];
    if (nextEvent > eventAddresses.size()) nextEvent = 0;

    for (size_t i = nextEvent; i < eventAddresses.size(); ++i) {
      if (maxEvents >= 0 && processedEvents >= maxEvents) return;
      if (currentSize >= 0 && static_cast<Long64_t>(eventAddresses[i] + 13) > currentSize) return;
      EventHeader event;
      if (!parseEventAt(in, eventAddresses[i], event)) {
        if (poll) return;
        continue;
      }
      const std::vector<RawDigi> digis = unpackITOFEvent(in, event, assembleIds, eleMap, clsb);
      const std::vector<OnlineHit> hits = makeHits(digis, geometry, calibration, 5., 21.5);
      updateObjects(event, digis, hits, file);
      nextEvent = i + 1;
      ++processedEvents;
      gSystem->ProcessEvents();
    }
  }

  void updateObjects(const EventHeader& event,
                     const std::vector<RawDigi>& digis,
                     const std::vector<OnlineHit>& hits,
                     const std::string& file)
  {
    fillUnpackerObjects(digis);
    latestHits = hits;
    std::vector<Double_t> latestHitPoints;
    const std::vector<EventHitCluster> eventClusters = mergedEventDisplayClusters(latestHits);
    const std::vector<TVector3> eventPoints = clusterPoints(eventClusters);
    const TrackFitResult alignTrack = reconstructEventDisplayTrackRansacResult(eventPoints);
    updateTrackAssistedAlignment(latestHits, eventClusters, eventPoints, alignTrack);
    updateGlobalCenterAlignment(latestHits);
    const std::vector<TVector3> eventTrack = trackAlgorithm == "RANSAC" ? alignTrack.line : reconstructEventDisplayTrackFast(eventPoints);
    latestHitPoints.reserve(std::min<size_t>(eventClusters.size(), 512) * 3);
    for (const TVector3& point : eventPoints) {
      if (latestHitPoints.size() >= 512 * 3) break;
      latestHitPoints.push_back(point.X());
      latestHitPoints.push_back(point.Y());
      latestHitPoints.push_back(point.Z());
    }
    for (size_t i = 0; i < latestHits.size(); ++i) {
      const auto& hit = latestHits[i];
      hit3D->Fill(hit.x, hit.y, hit.z);
      const auto posIt = detectorPosIndex.find(hit.det);
      if (posIt != detectorPosIndex.end()) {
        const TVector3 local = geometry.localPosition(hit.det, TVector3(hit.x, hit.y, hit.z));
        detectorPos[posIt->second]->Fill(local.X(), local.Y());
      }
      switch (geometry.wallGroup(hit.det)) {
      case 0:
        wallPosXn->Fill(hit.z, hit.y);
        break;
      case 1:
        wallPosZ->Fill(hit.x, hit.y);
        break;
      case 2:
        wallPosXp->Fill(hit.z, hit.y);
        break;
      default:
        break;
      }
    }
    if (latestHitPoints.empty()) {
      Double_t* emptyPoints = nullptr;
      latestHits3D->SetPolyMarker(0, emptyPoints, 20);
    } else {
      latestHits3D->SetPolyMarker(static_cast<Int_t>(latestHitPoints.size() / 3),
                                  latestHitPoints.data(),
                                  20);
    }
    latestHits3D->SetName("LatestHits3D");
    latestHits3D->SetMarkerColor(kRed + 1);
    latestHits3D->SetMarkerSize(3.0);
    if (eventTrack.size() >= 2) {
      Double_t trackPoints[6] = {
        eventTrack[0].X(), eventTrack[0].Y(), eventTrack[0].Z(),
        eventTrack[1].X(), eventTrack[1].Y(), eventTrack[1].Z()
      };
      latestTrack3D->SetPolyMarker(2, trackPoints, 20);
    } else {
      Double_t* emptyTrack = nullptr;
      latestTrack3D->SetPolyMarker(0, emptyTrack, 20);
    }
    latestTrack3D->SetName("LatestTrack3D");
    latestTrack3D->SetMarkerColor(kGreen + 2);
    latestTrack3D->SetMarkerSize(1.0);
    saveEventHistory(processedEvents + 1, latestHitPoints, eventTrack);
    for (const auto& digi : digis) {
      digiMap->Fill(digi.det, digi.strip);
    }
    updateChannelHealth(digis);
    hitMultiplicity->Fill(static_cast<double>(hits.size()));
    digiMultiplicity->Fill(static_cast<double>(digis.size()));

    std::ostringstream ss;
    ss << "file=" << file
       << " event=" << processedEvents
       << " digis=" << digis.size()
       << " hits=" << hits.size()
       << " track=" << trackAlgorithm
       << " align=" << (calibration.loaded ? "file" : "self")
       << " commonCenterCorr=" << std::fixed << std::setprecision(2)
       << calibration.commonPositionCorrectionCm << "cm"
       << " commonN=" << calibration.commonAlignCount;
    status->SetTitle(ss.str().c_str());
    status->SetText(0.02, 0.5, ss.str().c_str());
  }

  void updateChannelHealth(const std::vector<RawDigi>& digis)
  {
    for (const auto& digi : digis) {
      if (digi.det < 0 || digi.det >= geometry.nDet()) continue;
      if (digi.strip < 0 || digi.strip >= geometry.nStrips(digi.det)) continue;
      stripHitCounts[digi.det][digi.strip] += 1;
    }

    struct WarningItem {
      int det;
      int strip;
      long long count;
      double neighborAverage;
      double ratio;
      std::string type;
    };
    std::vector<WarningItem> warnings;
    int readyDetectors = 0;
    const int activationThreshold = 100;
    const int maxWarnings = 24;

    for (int det : mappedDetectors) {
      const auto detIt = stripHitCounts.find(det);
      if (detIt == stripHitCounts.end()) continue;
      long long maxCount = 0;
      for (const auto& item : detIt->second) maxCount = std::max(maxCount, item.second);
      if (maxCount < activationThreshold) continue;
      ++readyDetectors;

      const std::set<int>& detStrips = mappedStripsByDetector[det].empty() ? mappedStrips : mappedStripsByDetector[det];
      std::set<int> highOutlierStrips;
      for (int strip : detStrips) {
        const int leftStrip = strip - 1;
        const int rightStrip = strip + 1;
        const auto leftIt = detIt->second.find(leftStrip);
        const auto rightIt = detIt->second.find(rightStrip);
        if (leftIt == detIt->second.end() || rightIt == detIt->second.end()) continue;
        const double neighborAverage = 0.5 * (static_cast<double>(leftIt->second) + static_cast<double>(rightIt->second));
        if (neighborAverage <= 0.) continue;
        const long long count = detIt->second.count(strip) ? detIt->second.at(strip) : 0;
        const double ratio = static_cast<double>(count) / neighborAverage;
        if (ratio > 5.) highOutlierStrips.insert(strip);
      }

      for (int strip : detStrips) {
        const int neighborStrips[2] = {strip - 1, strip + 1};
        double neighborSum = 0.;
        int neighborUsed = 0;
        for (int neighborStrip : neighborStrips) {
          if (detStrips.find(neighborStrip) == detStrips.end()) continue;
          if (highOutlierStrips.find(neighborStrip) != highOutlierStrips.end()) continue;
          const auto neighborIt = detIt->second.find(neighborStrip);
          neighborSum += neighborIt == detIt->second.end() ? 0. : static_cast<double>(neighborIt->second);
          ++neighborUsed;
        }
        if (neighborUsed <= 0) continue;
        const double neighborAverage = neighborSum / static_cast<double>(neighborUsed);
        if (neighborAverage <= 0.) continue;
        const long long count = detIt->second.count(strip) ? detIt->second.at(strip) : 0;
        const double ratio = static_cast<double>(count) / neighborAverage;
        if (ratio > 5.) {
          warnings.push_back({det, strip, count, neighborAverage, ratio, "high"});
        } else if (ratio < 0.2) {
          warnings.push_back({det, strip, count, neighborAverage, ratio, "low"});
        }
      }
    }

    std::sort(warnings.begin(), warnings.end(), [](const WarningItem& a, const WarningItem& b) {
      const double sa = a.type == "high" ? a.ratio : (a.ratio > 0. ? 1. / a.ratio : 999.);
      const double sb = b.type == "high" ? b.ratio : (b.ratio > 0. ? 1. / b.ratio : 999.);
      return sa > sb;
    });
    if (warnings.size() > static_cast<size_t>(maxWarnings)) warnings.resize(maxWarnings);

    std::ostringstream json;
    json << "{\"level\":\"" << (warnings.empty() ? (readyDetectors > 0 ? "ok" : "waiting") : "warning")
         << "\",\"ready_detectors\":" << readyDetectors
         << ",\"detectors\":" << mappedDetectors.size()
         << ",\"activation_threshold\":" << activationThreshold
         << ",\"warnings\":[";
    for (size_t i = 0; i < warnings.size(); ++i) {
      const WarningItem& item = warnings[i];
      if (i) json << ",";
      json << "{\"det\":" << item.det
           << ",\"strip\":" << item.strip
           << ",\"count\":" << item.count
           << ",\"neighbor_avg\":" << std::fixed << std::setprecision(1) << item.neighborAverage
           << ",\"ratio\":" << std::fixed << std::setprecision(2) << item.ratio
           << ",\"type\":\"" << item.type << "\"}";
    }
    json << "]}";
    const std::string text = json.str();
    channelHealthStatus->SetTitle(text.c_str());
    channelHealthStatus->SetText(0.02, 0.5, text.c_str());
  }

  void fillUnpackerObjects(const std::vector<RawDigi>& digis)
  {
    for (const auto& digi : digis) {
      const double totPs = digi.tot * 1000.;
      const auto channelIt = channelHistIndex.find(std::make_pair(digi.fee, digi.channel));
      if (channelIt != channelHistIndex.end()) {
        const size_t idx = channelIt->second;
        totChannel[idx]->Fill(totPs);
        leadingChannel[idx]->Fill(digi.leadingRelNs);
        doubleChainTimeDiff[idx]->Fill(digi.doubleChainDiffPs);
      }
      const auto feeIt = feeHistIndex.find(digi.fee);
      if (feeIt != feeHistIndex.end()) {
        countChannel[feeIt->second]->Fill(digi.channel);
        totFee[feeIt->second]->Fill(digi.channel, totPs);
      }
      const auto detSideIt = detectorSideHistIndex.find(std::make_pair(digi.det, digi.side));
      if (detSideIt != detectorSideHistIndex.end()) {
        totDet[detSideIt->second]->Fill(digi.strip, totPs);
      }
      const auto leadingIt = leadingTimeIndex.find(digi.det);
      if (leadingIt != leadingTimeIndex.end()) {
        leadingTime[leadingIt->second]->Fill(digi.leadingRelNs * 1000.);
      }
      totAll->Fill(totPs);
    }
  }

  TVector3 eventDisplayPosition(const OnlineHit& hit) const
  {
    const TVector3 local = geometry.localPosition(hit.det, TVector3(hit.x, hit.y, hit.z));
    return TVector3(local.X(), local.Y(), kEventDisplayLayerSpacingCm * eventDisplayLayerIndex(hit.det));
  }

  std::vector<EventHitCluster> mergedEventDisplayClusters(const std::vector<OnlineHit>& hits) const
  {
    std::vector<const OnlineHit*> selected;
    selected.reserve(hits.size());
    for (const auto& hit : hits) {
      if (hit.det >= 0 && mappedDetectors.count(hit.det) && hit.strip >= 0) selected.push_back(&hit);
    }
    std::sort(selected.begin(), selected.end(), [](const OnlineHit* a, const OnlineHit* b) {
      if (a->det != b->det) return a->det < b->det;
      return a->strip < b->strip;
    });

    std::vector<EventHitCluster> clusters;
    for (const OnlineHit* hit : selected) {
      const TVector3 pos = eventDisplayPosition(*hit);
      const double weight = hit->tot > 0. ? hit->tot : 1.;
      if (clusters.empty() ||
          clusters.back().det != hit->det ||
          hit->strip > clusters.back().lastStrip + 1) {
        EventHitCluster cluster;
        cluster.det = hit->det;
        cluster.firstStrip = hit->strip;
        cluster.lastStrip = hit->strip;
        cluster.x = pos.X() * weight;
        cluster.y = pos.Y() * weight;
        cluster.z = pos.Z() * weight;
        cluster.weight = weight;
        cluster.count = 1;
        clusters.push_back(cluster);
      } else {
        EventHitCluster& cluster = clusters.back();
        cluster.lastStrip = hit->strip;
        cluster.x += pos.X() * weight;
        cluster.y += pos.Y() * weight;
        cluster.z += pos.Z() * weight;
        cluster.weight += weight;
        ++cluster.count;
      }
    }
    return clusters;
  }

  std::vector<TVector3> clusterPoints(const std::vector<EventHitCluster>& clusters) const
  {
    std::vector<TVector3> points;
    points.reserve(clusters.size());
    for (const EventHitCluster& cluster : clusters) {
      const double weight = cluster.weight > 0. ? cluster.weight : static_cast<double>(std::max(1, cluster.count));
      points.push_back(TVector3(cluster.x / weight, cluster.y / weight, cluster.z / weight));
    }
    return points;
  }

  std::vector<TVector3> mergedEventDisplayPoints(const std::vector<OnlineHit>& hits) const
  {
    return clusterPoints(mergedEventDisplayClusters(hits));
  }

  std::vector<TVector3> reconstructEventDisplayTrack(const std::vector<TVector3>& points) const
  {
    if (trackAlgorithm == "RANSAC") return reconstructEventDisplayTrackRansacResult(points).line;
    return reconstructEventDisplayTrackFast(points);
  }

  std::vector<TVector3> reconstructEventDisplayTrackFast(const std::vector<TVector3>& points) const
  {
    std::vector<size_t> indices;
    indices.reserve(points.size());
    for (size_t i = 0; i < points.size(); ++i) indices.push_back(i);
    return fitEventDisplayLine(points, indices);
  }

  std::vector<TVector3> fitEventDisplayLine(const std::vector<TVector3>& points,
                                            const std::vector<size_t>& indices) const
  {
    if (indices.size() < 2) return {};
    const double trackZMin = -8.;
    const double trackZMax = 55.;
    if (indices.size() == 2) {
      const TVector3& first = points[indices[0]];
      const TVector3& second = points[indices[1]];
      const double dz = second.Z() - first.Z();
      if (std::fabs(dz) <= 1.e-9) return {first, second};
      const double dxdz = (second.X() - first.X()) / dz;
      const double dydz = (second.Y() - first.Y()) / dz;
      const auto pointAtZ = [&](double z) {
        const double deltaZ = z - first.Z();
        return TVector3(first.X() + dxdz * deltaZ,
                        first.Y() + dydz * deltaZ,
                        z);
      };
      return {pointAtZ(trackZMin), pointAtZ(trackZMax)};
    }

    double zMean = 0.;
    for (const size_t index : indices) {
      zMean += points[index].Z();
    }
    zMean /= static_cast<double>(indices.size());

    double szz = 0.;
    double sx = 0.;
    double sy = 0.;
    double sxz = 0.;
    double syz = 0.;
    for (const size_t index : indices) {
      const TVector3& point = points[index];
      const double dz = point.Z() - zMean;
      szz += dz * dz;
      sx += point.X();
      sy += point.Y();
      sxz += point.X() * dz;
      syz += point.Y() * dz;
    }
    if (szz <= 1.e-9) return {points[indices.front()], points[indices.back()]};

    const double x0 = sx / static_cast<double>(indices.size());
    const double y0 = sy / static_cast<double>(indices.size());
    const double dxdz = sxz / szz;
    const double dydz = syz / szz;
    const auto pointAtZ = [&](double z) {
      const double dz = z - zMean;
      return TVector3(x0 + dxdz * dz, y0 + dydz * dz, z);
    };
    return {pointAtZ(trackZMin), pointAtZ(trackZMax)};
  }

  double eventDisplayTrackResidual(const TVector3& point,
                                   const std::vector<TVector3>& track) const
  {
    if (track.size() < 2) return 1.e30;
    const double dz = track[1].Z() - track[0].Z();
    if (std::fabs(dz) <= 1.e-9) return 1.e30;
    const double frac = (point.Z() - track[0].Z()) / dz;
    const double x = track[0].X() + (track[1].X() - track[0].X()) * frac;
    const double y = track[0].Y() + (track[1].Y() - track[0].Y()) * frac;
    const double dx = point.X() - x;
    const double dy = point.Y() - y;
    return std::sqrt(dx * dx + dy * dy);
  }

  bool eventDisplayTrackPointAtZ(const std::vector<TVector3>& track,
                                 double z,
                                 TVector3& out) const
  {
    if (track.size() < 2) return false;
    const double dz = track[1].Z() - track[0].Z();
    if (std::fabs(dz) <= 1.e-9) return false;
    const double frac = (z - track[0].Z()) / dz;
    out = TVector3(track[0].X() + (track[1].X() - track[0].X()) * frac,
                   track[0].Y() + (track[1].Y() - track[0].Y()) * frac,
                   z);
    return true;
  }

  void updateTrackAssistedAlignment(const std::vector<OnlineHit>& hits,
                                    const std::vector<EventHitCluster>& clusters,
                                    const std::vector<TVector3>& points,
                                    const TrackFitResult& track)
  {
    if (!calibration.usingSelfAlignment() || !track.reliable || track.inliers.size() < 3) return;
    for (const OnlineHit& hit : hits) {
      if (hit.det < 0 || hit.det >= geometry.nDet() || hit.strip < 0) continue;
      const TVector3 measured = eventDisplayPosition(hit);
      if (eventDisplayTrackResidual(measured, track.line) > 5.0) continue;

      std::vector<size_t> leaveOne;
      leaveOne.reserve(track.inliers.size());
      for (size_t index : track.inliers) {
        if (index >= clusters.size()) continue;
        if (clusters[index].det == hit.det) continue;
        leaveOne.push_back(index);
      }
      if (leaveOne.size() < 2) continue;
      const std::vector<TVector3> leaveTrack = fitEventDisplayLine(points, leaveOne);
      TVector3 predicted;
      if (!eventDisplayTrackPointAtZ(leaveTrack, measured.Z(), predicted)) continue;
      const double residualX = measured.X() - predicted.X();
      const double residualY = measured.Y() - predicted.Y();
      if (std::fabs(residualX) > 6.0 || std::fabs(residualY) > 6.0) continue;
      calibration.updateTrackResidualAlignment(hit.det, hit.strip, residualX);
    }
  }

  void updateGlobalCenterAlignment(const std::vector<OnlineHit>& hits)
  {
    if (!calibration.usingSelfAlignment()) return;
    for (const OnlineHit& hit : hits) {
      if (hit.det < 0 || hit.det >= geometry.nDet() || hit.strip < 0) continue;
      const TVector3 local = geometry.localPosition(hit.det, TVector3(hit.x, hit.y, hit.z));
      calibration.addGlobalCenterSample(hit.det, local.X());
    }
    calibration.updateGlobalCenterAnchor();
  }

  std::vector<size_t> eventDisplayInliers(const std::vector<TVector3>& points,
                                          const std::vector<TVector3>& track,
                                          double threshold,
                                          double& chi2) const
  {
    chi2 = 0.;
    std::map<int, std::pair<double, size_t>> bestByDetector;
    for (size_t i = 0; i < points.size(); ++i) {
      const double residual = eventDisplayTrackResidual(points[i], track);
      if (residual > threshold) continue;
      const int detector = eventDisplayDetectorFromZ(points[i].Z());
      auto iter = bestByDetector.find(detector);
      if (iter == bestByDetector.end() || residual < iter->second.first) {
        bestByDetector[detector] = std::make_pair(residual, i);
      }
    }

    std::vector<size_t> indices;
    indices.reserve(bestByDetector.size());
    for (const auto& item : bestByDetector) {
      indices.push_back(item.second.second);
      chi2 += item.second.first * item.second.first;
    }
    return indices;
  }

  std::vector<TVector3> reconstructEventDisplayTrackRansac(const std::vector<TVector3>& points) const
  {
    return reconstructEventDisplayTrackRansacResult(points).line;
  }

  TrackFitResult reconstructEventDisplayTrackRansacResult(const std::vector<TVector3>& points) const
  {
    TrackFitResult result;
    if (points.size() < 2) return result;
    if (points.size() == 2) {
      result.line = reconstructEventDisplayTrackFast(points);
      result.inliers = {0, 1};
      result.chi2 = 0.;
      result.reliable = false;
      return result;
    }

    const double inlierThresholdCm = 3.0;
    std::vector<size_t> bestInliers;
    std::vector<TVector3> bestTrack;
    double bestChi2 = 1.e300;

    for (size_t i = 0; i < points.size(); ++i) {
      for (size_t j = i + 1; j < points.size(); ++j) {
        if (std::fabs(points[i].Z() - points[j].Z()) < 1.e-4) continue;
        std::vector<size_t> sample;
        sample.push_back(i);
        sample.push_back(j);
        const std::vector<TVector3> candidate = fitEventDisplayLine(points, sample);
        double candidateChi2 = 0.;
        std::vector<size_t> inliers = eventDisplayInliers(points, candidate, inlierThresholdCm, candidateChi2);
        if (inliers.size() < 2) continue;

        std::vector<TVector3> refined = fitEventDisplayLine(points, inliers);
        double refinedChi2 = 0.;
        inliers = eventDisplayInliers(points, refined, inlierThresholdCm, refinedChi2);
        if (inliers.size() > bestInliers.size() ||
            (inliers.size() == bestInliers.size() && refinedChi2 < bestChi2)) {
          bestInliers = inliers;
          bestTrack = refined;
          bestChi2 = refinedChi2;
        }
      }
    }

    if (bestInliers.size() >= 3) {
      result.line = fitEventDisplayLine(points, bestInliers);
      result.inliers = bestInliers;
      result.chi2 = bestChi2;
      result.reliable = true;
      return result;
    }
    result.line = reconstructEventDisplayTrackFast(points);
    result.reliable = false;
    return result;
  }

  void setMarkerPoints(TPolyMarker3D* marker,
                       const std::vector<Double_t>& points,
                       Int_t style,
                       Int_t color,
                       Double_t size)
  {
    if (!marker) return;
    if (points.empty()) {
      Double_t* empty = nullptr;
      marker->SetPolyMarker(0, empty, style);
    } else {
      marker->SetPolyMarker(static_cast<Int_t>(points.size() / 3),
                            const_cast<Double_t*>(points.data()),
                            style);
    }
    marker->SetMarkerStyle(style);
    marker->SetMarkerColor(color);
    marker->SetMarkerSize(size);
  }

  void saveEventHistory(Long64_t eventSerial,
                        const std::vector<Double_t>& hitPoints,
                        const std::vector<TVector3>& trackPoints)
  {
    if (eventSerial <= 0 || eventHistoryHits.empty() || eventHistoryTracks.empty()) return;
    const int slot = static_cast<int>((eventSerial - 1) % kEventHistoryCapacity);
    setMarkerPoints(eventHistoryHits[slot].get(), hitPoints, 20, kRed + 1, 3.0);
    eventHistoryHits[slot]->SetName(Form("EventHits_%03d", slot));
    eventHistoryHits[slot]->SetUniqueID(static_cast<UInt_t>(eventSerial));

    std::vector<Double_t> track;
    if (trackPoints.size() >= 2) {
      track.reserve(6);
      track.push_back(trackPoints[0].X());
      track.push_back(trackPoints[0].Y());
      track.push_back(trackPoints[0].Z());
      track.push_back(trackPoints[1].X());
      track.push_back(trackPoints[1].Y());
      track.push_back(trackPoints[1].Z());
    }
    setMarkerPoints(eventHistoryTracks[slot].get(), track, 20, kGreen + 2, 1.0);
    eventHistoryTracks[slot]->SetName(Form("EventTrack_%03d", slot));
    eventHistoryTracks[slot]->SetUniqueID(static_cast<UInt_t>(eventSerial));

    eventHistoryLatest = eventSerial;
    eventHistoryCount = std::min(static_cast<int>(kEventHistoryCapacity), eventHistoryCount + 1);
    const Long64_t first = eventHistoryLatest - eventHistoryCount + 1;
    const std::string title = Form("capacity=%d latest=%lld first=%lld count=%d slot=%d",
                                   kEventHistoryCapacity,
                                   static_cast<long long>(eventHistoryLatest),
                                   static_cast<long long>(first),
                                   eventHistoryCount,
                                   slot);
    eventHistoryStatus->SetTitle(title.c_str());
    eventHistoryStatus->SetText(0.02, 0.5, title.c_str());
  }

  std::string input;
  std::string calibFile;
  int httpPortValue = 8090;
  Long64_t maxEvents = -1;
  int refreshMillis = 1000;
  bool poll = false;
  std::unique_ptr<THttpServer> server;
  std::unique_ptr<TH2D> wallPosZ;
  std::unique_ptr<TH2D> wallPosXn;
  std::unique_ptr<TH2D> wallPosXp;
  std::unique_ptr<TH3D> hit3D;
  std::unique_ptr<TH2D> digiMap;
  std::vector<std::unique_ptr<TH2D>> detectorPos;
  std::unique_ptr<TH1D> hitMultiplicity;
  std::unique_ptr<TH1D> digiMultiplicity;
  std::vector<std::unique_ptr<TH1D>> totChannel;
  std::vector<std::unique_ptr<TH1D>> leadingChannel;
  std::vector<std::unique_ptr<TH1D>> doubleChainTimeDiff;
  std::vector<std::unique_ptr<TH1D>> countChannel;
  std::vector<std::unique_ptr<TH2D>> totFee;
  std::vector<std::unique_ptr<TH2D>> totDet;
  std::vector<std::unique_ptr<TH1D>> leadingTime;
  std::set<int> mappedDetectors;
  std::set<int> mappedStrips;
  std::set<int> mappedSides;
  std::set<int> mappedFees;
  std::set<int> mappedChannels;
  std::map<int, std::set<int>> mappedChannelsByFee;
  std::map<int, std::set<int>> mappedSidesByDetector;
  std::map<int, std::set<int>> mappedStripsByDetector;
  std::map<int, size_t> detectorPosIndex;
  std::map<int, size_t> feeHistIndex;
  std::map<std::pair<int, int>, size_t> channelHistIndex;
  std::map<std::pair<int, int>, size_t> detectorSideHistIndex;
  std::map<int, size_t> leadingTimeIndex;
  std::unique_ptr<TH1D> totAll;
  std::unique_ptr<TPolyMarker3D> latestHits3D;
  std::unique_ptr<TPolyMarker3D> latestTrack3D;
  std::vector<std::unique_ptr<TPolyMarker3D>> eventHistoryHits;
  std::vector<std::unique_ptr<TPolyMarker3D>> eventHistoryTracks;
  std::unique_ptr<TText> eventHistoryStatus;
  std::unique_ptr<TText> channelHealthStatus;
  std::unique_ptr<TText> status;
  TFile* eventGeoFile = nullptr;
  TGeoManager* eventGeoManager = nullptr;
  TGeoVolume* eventTopVolume = nullptr;
  std::vector<TGeoVolume*> eventDisplayVolumes;
  std::unordered_map<int, EleMapEntry> eleMap;
  std::unordered_map<uint64_t, std::vector<double>> clsb;
  std::set<uint64_t> assembleIds;
  std::map<std::string, size_t> nextEventIndex;
  std::map<int, std::map<int, long long>> stripHitCounts;
  std::string trackAlgorithm = "Fast";
  CalibrationTable calibration;
  NomagGeometry geometry;
  std::vector<OnlineHit> latestHits;
  Long64_t processedEvents = 0;
  Long64_t eventHistoryLatest = 0;
  int eventHistoryCount = 0;
};

} // namespace

void iTOFRootOnlineMonitor(const char* input,
                           int httpPort = 8090,
                           Long64_t maxEvents = -1,
                           int refreshMs = 1000,
                           bool pollInput = false,
                           const char* calibFile = "Calib_iTOF.root")
{
  if (!input || std::string(input).empty()) {
    std::cerr << "Usage: root -l 'itof/online/iTOFRootOnlineMonitor.C(\"input.dat|input.list|dir\",8090,-1,1000,true,\"Calib_iTOF.root\")'" << std::endl;
    return;
  }
  ITOFRootOnlineMonitor monitor(input, httpPort, maxEvents, refreshMs, pollInput, calibFile ? calibFile : "");
  monitor.run();
}
