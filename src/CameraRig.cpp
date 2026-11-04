#pragma push_macro("emit")
#undef emit
#include "pxr/pxr.h"
#include "pxr/base/gf/vec2f.h"
#include "pxr/base/gf/vec3d.h"
#include "pxr/base/gf/vec3f.h"
#include "pxr/base/vt/array.h"
#include "pxr/base/vt/value.h"
#include "pxr/usd/sdf/path.h"
#include "pxr/usd/usd/attribute.h"
#include "pxr/usd/usd/relationship.h"
#include "pxr/usd/usd/stage.h"
#include "pxr/usd/usd/timeCode.h"
#include "pxr/usd/usdGeom/basisCurves.h"
#include "pxr/usd/usdGeom/camera.h"
#include "pxr/usd/usdGeom/tokens.h"
#include "pxr/usd/usdGeom/xform.h"
#include "pxr/usd/usdGeom/xformable.h"
#pragma pop_macro("emit")

#include "CameraRig.h"
#include <QJsonArray>

#include <cmath>
#include <algorithm>
#include <QCryptographicHash>

using namespace pxr;

namespace {

struct SplineSegmentResult {
    QVector3D position;
    QVector3D tangent;
};

SplineSegmentResult catmullRomSegment(
    const QVector3D& p0, const QVector3D& p1, const QVector3D& p2, const QVector3D& p3,
    double u, double alpha = 0.5) {
    auto knot = [alpha](const QVector3D& a, const QVector3D& b) -> double {
        const double dist = static_cast<double>((b - a).length());
        return std::max(1e-5, std::pow(dist, alpha));
    };

    const double t0 = 0.0;
    const double t1 = t0 + knot(p0, p1);
    const double t2 = t1 + knot(p1, p2);
    const double t3 = t2 + knot(p2, p3);

    const double t = t1 + u * (t2 - t1);

    const QVector3D a1 = static_cast<float>((t1 - t) / (t1 - t0)) * p0 + static_cast<float>((t - t0) / (t1 - t0)) * p1;
    const QVector3D a2 = static_cast<float>((t2 - t) / (t2 - t1)) * p1 + static_cast<float>((t - t1) / (t2 - t1)) * p2;
    const QVector3D a3 = static_cast<float>((t3 - t) / (t3 - t2)) * p2 + static_cast<float>((t - t2) / (t3 - t2)) * p3;

    const QVector3D b1 = static_cast<float>((t2 - t) / (t2 - t0)) * a1 + static_cast<float>((t - t0) / (t2 - t0)) * a2;
    const QVector3D b2 = static_cast<float>((t3 - t) / (t3 - t1)) * a2 + static_cast<float>((t - t1) / (t3 - t1)) * a3;

    const QVector3D c = static_cast<float>((t2 - t) / (t2 - t1)) * b1 + static_cast<float>((t - t1) / (t2 - t1)) * b2;

    const double eps = 1e-4;
    const double tNext = t + eps * (t2 - t1);
    const QVector3D a1n = static_cast<float>((t1 - tNext) / (t1 - t0)) * p0 + static_cast<float>((tNext - t0) / (t1 - t0)) * p1;
    const QVector3D a2n = static_cast<float>((t2 - tNext) / (t2 - t1)) * p1 + static_cast<float>((tNext - t1) / (t2 - t1)) * p2;
    const QVector3D a3n = static_cast<float>((t3 - tNext) / (t3 - t2)) * p2 + static_cast<float>((tNext - t2) / (t3 - t2)) * p3;

    const QVector3D b1n = static_cast<float>((t2 - tNext) / (t2 - t0)) * a1n + static_cast<float>((tNext - t0) / (t2 - t0)) * a2n;
    const QVector3D b2n = static_cast<float>((t3 - tNext) / (t3 - t1)) * a2n + static_cast<float>((tNext - t1) / (t3 - t1)) * a3n;

    const QVector3D cn = static_cast<float>((t2 - tNext) / (t2 - t1)) * b1n + static_cast<float>((tNext - t1) / (t2 - t1)) * b2n;

    QVector3D tan = (cn - c) / static_cast<float>(eps * std::max(1e-5, t2 - t1));
    if (tan.lengthSquared() > 1e-6f) {
        tan.normalize();
    } else {
        tan = QVector3D(0.0f, 0.0f, -1.0f);
    }
    return {c, tan};
}

class SplineLut {
public:
    explicit SplineLut(const QVector<QVector3D>& pts, int samplesPerSegment = 64) {
        if (pts.isEmpty()) {
            myPoints.append(QVector3D(0.0f, 1.6f, 5.0f));
        } else {
            myPoints = pts;
        }

        const int n = myPoints.size();
        if (n < 2) {
            myTotalLength = 0.0;
            myDistances = {0.0, 1.0};
            mySamples = {{0, 0.0}, {0, 1.0}};
            return;
        }

        myDistances.append(0.0);
        mySamples.append({0, 0.0});

        double cumDist = 0.0;
        QVector3D prevPos = myPoints[0];

        for (int seg = 0; seg < n - 1; ++seg) {
            QVector3D p0, p1, p2, p3;
            get4Points(seg, p0, p1, p2, p3);

            for (int s = 1; s <= samplesPerSegment; ++s) {
                const double u = static_cast<double>(s) / static_cast<double>(samplesPerSegment);
                const SplineSegmentResult res = catmullRomSegment(p0, p1, p2, p3, u);
                cumDist += static_cast<double>((res.position - prevPos).length());
                myDistances.append(cumDist);
                mySamples.append({seg, u});
                prevPos = res.position;
            }
        }
        myTotalLength = std::max(1e-6, cumDist);
    }

    double totalLength() const { return myTotalLength; }

    SplineSegmentResult sampleAtDistance(double dist) const {
        if (myPoints.size() < 2) {
            return {myPoints[0], QVector3D(0.0f, 0.0f, -1.0f)};
        }

        const double clamped = std::clamp(dist, 0.0, myTotalLength);
        auto it = std::lower_bound(myDistances.begin(), myDistances.end(), clamped);
        int idx = static_cast<int>(std::distance(myDistances.begin(), it));
        int seg = 0;
        double u = 0.0;

        if (idx <= 0) {
            seg = mySamples.first().first;
            u = mySamples.first().second;
        } else if (idx >= myDistances.size()) {
            seg = mySamples.last().first;
            u = mySamples.last().second;
        } else {
            const double d0 = myDistances[idx - 1];
            const double d1 = myDistances[idx];
            const double ratio = (clamped - d0) / std::max(1e-6, d1 - d0);
            const auto& s0 = mySamples[idx - 1];
            const auto& s1 = mySamples[idx];
            if (s0.first == s1.first) {
                seg = s0.first;
                u = s0.second + ratio * (s1.second - s0.second);
            } else {
                seg = (ratio > 0.5) ? s1.first : s0.first;
                u = (ratio > 0.5) ? s1.second : s0.second;
            }
        }

        QVector3D p0, p1, p2, p3;
        get4Points(seg, p0, p1, p2, p3);
        return catmullRomSegment(p0, p1, p2, p3, u);
    }

private:
    void get4Points(int seg, QVector3D& p0, QVector3D& p1, QVector3D& p2, QVector3D& p3) const {
        const int n = myPoints.size();
        p1 = myPoints[seg];
        p2 = myPoints[seg + 1];
        p0 = (seg == 0) ? (2.0f * p1 - p2) : myPoints[seg - 1];
        p3 = (seg + 2 < n) ? myPoints[seg + 2] : (2.0f * p2 - p1);
    }

    QVector<QVector3D> myPoints;
    double myTotalLength = 0.0;
    QVector<double> myDistances;
    QVector<QPair<int, double>> mySamples;
};

void evaluateSeededShakeCpp(double timeS, int seed, double amp, double freq, QVector3D& outPosOffset, QVector3D& outRotOffset) {
    if (amp <= 1e-5) {
        outPosOffset = QVector3D();
        outRotOffset = QVector3D();
        return;
    }

    QByteArray hashData = QString("shake_seed_%1").arg(seed).toUtf8();
    QByteArray hash = QCryptographicHash::hash(hashData, QCryptographicHash::Sha256);

    double phases[12];
    for (int i = 0; i < 12; ++i) {
        phases[i] = (static_cast<uint8_t>(hash.at(i)) / 255.0) * 2.0 * M_PI;
    }

    const double t = timeS;
    const double f = freq;

    const float dx = static_cast<float>(amp * 0.06 * (std::sin(f * 2.0 * M_PI * t + phases[0]) + 0.5 * std::sin(f * 3.7 * M_PI * t + phases[1])));
    const float dy = static_cast<float>(amp * 0.06 * (std::sin(f * 2.4 * M_PI * t + phases[2]) + 0.5 * std::sin(f * 4.1 * M_PI * t + phases[3])));
    const float dz = static_cast<float>(amp * 0.03 * (std::sin(f * 1.9 * M_PI * t + phases[4]) + 0.5 * std::sin(f * 3.2 * M_PI * t + phases[5])));

    const float dpitch = static_cast<float>(amp * 1.2 * (std::sin(f * 2.1 * M_PI * t + phases[6]) + 0.5 * std::sin(f * 3.5 * M_PI * t + phases[7])));
    const float dyaw   = static_cast<float>(amp * 1.2 * (std::sin(f * 1.8 * M_PI * t + phases[8]) + 0.5 * std::sin(f * 4.3 * M_PI * t + phases[9])));
    const float droll  = static_cast<float>(amp * 0.8 * (std::sin(f * 2.5 * M_PI * t + phases[10]) + 0.5 * std::sin(f * 3.9 * M_PI * t + phases[11])));

    outPosOffset = QVector3D(dx, dy, dz);
    outRotOffset = QVector3D(dpitch, dyaw, droll);
}

} // namespace

CameraRigEvaluator::CameraRigEvaluator(double fps) : myFps(fps > 0.0 ? fps : 24.0) {}

double CameraRigEvaluator::evaluateEase(double tNorm, CameraEaseType ease) {
    const double t = std::clamp(tNorm, 0.0, 1.0);
    switch (ease) {
        case CameraEaseType::Linear:
            return t;
        case CameraEaseType::EaseIn:
            return t * t;
        case CameraEaseType::EaseOut:
            return t * (2.0 - t);
        case CameraEaseType::EaseInOut:
        default:
            return 0.5 * (1.0 - std::cos(M_PI * t));
    }
}

void CameraRigEvaluator::sensorDimensions(CameraSensorPreset preset, double& outHoriz, double& outVert) {
    switch (preset) {
        case CameraSensorPreset::Super35:
            outHoriz = 24.89;
            outVert = 18.66;
            break;
        case CameraSensorPreset::FullFrame:
            outHoriz = 36.0;
            outVert = 24.0;
            break;
        case CameraSensorPreset::Standard16_9:
        default:
            outHoriz = 36.0;
            outVert = 20.25;
            break;
    }
}

double CameraRigEvaluator::computeShotSizeDistance(CameraShotSize size, double subjectHeight, double focalLength, double verticalAperture) const {
    double targetExtent = 1.4;
    switch (size) {
        case CameraShotSize::ExtremeWide: targetExtent = 16.0; break;
        case CameraShotSize::Wide: targetExtent = 3.0; break;
        case CameraShotSize::Medium: targetExtent = 1.4; break;
        case CameraShotSize::MediumCloseUp: targetExtent = 0.85; break;
        case CameraShotSize::CloseUp: targetExtent = 0.45; break;
        case CameraShotSize::ExtremeCloseUp: targetExtent = 0.18; break;
        case CameraShotSize::OverTheShoulder: targetExtent = 1.4; break;
        case CameraShotSize::TopDown: targetExtent = 3.5; break;
    }
    const double scaledExtent = targetExtent * std::max(0.2, subjectHeight / 1.8);
    return std::max(0.5, (scaledExtent * focalLength) / std::max(1.0, verticalAperture));
}

double CameraRigEvaluator::computeShotSizeFocal(CameraShotSize size, double subjectHeight, double distance, double verticalAperture) const {
    double targetExtent = 1.4;
    switch (size) {
        case CameraShotSize::ExtremeWide: targetExtent = 16.0; break;
        case CameraShotSize::Wide: targetExtent = 3.0; break;
        case CameraShotSize::Medium: targetExtent = 1.4; break;
        case CameraShotSize::MediumCloseUp: targetExtent = 0.85; break;
        case CameraShotSize::CloseUp: targetExtent = 0.45; break;
        case CameraShotSize::ExtremeCloseUp: targetExtent = 0.18; break;
        case CameraShotSize::OverTheShoulder: targetExtent = 1.4; break;
        case CameraShotSize::TopDown: targetExtent = 3.5; break;
    }
    const double scaledExtent = targetExtent * std::max(0.2, subjectHeight / 1.8);
    const double focal = (distance * verticalAperture) / std::max(0.05, scaledExtent);
    return std::clamp(focal, 12.0, 200.0);
}

QVector3D CameraRigEvaluator::computeLookAtRotation(const QVector3D& camPos, const QVector3D& targetPos, double rollDeg) const {
    const double dx = static_cast<double>(targetPos.x() - camPos.x());
    const double dy = static_cast<double>(targetPos.y() - camPos.y());
    const double dz = static_cast<double>(targetPos.z() - camPos.z());
    const double horizDist = std::hypot(dx, dz);

    if (horizDist < 1e-4 && std::abs(dy) < 1e-4) {
        return QVector3D(0.0f, 0.0f, static_cast<float>(rollDeg));
    }
    if (horizDist < 1e-4) {
        const float pitch = (dy < 0.0) ? -90.0f : 90.0f;
        return QVector3D(pitch, 0.0f, static_cast<float>(rollDeg));
    }

    const double yawDeg = std::atan2(-dx, -dz) * (180.0 / M_PI);
    const double pitchDeg = std::atan2(dy, horizDist) * (180.0 / M_PI);
    return QVector3D(static_cast<float>(pitchDeg), static_cast<float>(yawDeg), static_cast<float>(rollDeg));
}

QVector3D CameraRigEvaluator::computePathTangentRotation(const QVector3D& tangent, double rollDeg) const {
    const double dx = static_cast<double>(tangent.x());
    const double dy = static_cast<double>(tangent.y());
    const double dz = static_cast<double>(tangent.z());
    const double horizDist = std::hypot(dx, dz);

    if (horizDist < 1e-4) {
        const float pitch = (dy < 0.0) ? -90.0f : 90.0f;
        return QVector3D(pitch, 0.0f, static_cast<float>(rollDeg));
    }
    const double yawDeg = std::atan2(-dx, -dz) * (180.0 / M_PI);
    const double pitchDeg = std::atan2(dy, horizDist) * (180.0 / M_PI);
    return QVector3D(static_cast<float>(pitchDeg), static_cast<float>(yawDeg), static_cast<float>(rollDeg));
}

QVector<QVector3D> CameraRigEvaluator::samplePolyline(const CameraRigData& rig, int samplesCount) const {
    QVector<QVector3D> result;
    if (rig.points.isEmpty()) return result;
    if (rig.points.size() == 1) {
        result.append(rig.points[0].position);
        return result;
    }

    QVector<QVector3D> raw;
    for (const auto& p : rig.points) raw.append(p.position);
    SplineLut lut(raw, 32);

    const int count = std::max(2, samplesCount);
    for (int i = 0; i < count; ++i) {
        const double d = (static_cast<double>(i) / (count - 1)) * lut.totalLength();
        result.append(lut.sampleAtDistance(d).position);
    }
    return result;
}

CameraSample CameraRigEvaluator::evaluate(
    const CameraRigData& rig,
    double timeS,
    const QHash<QString, QVector3D>& actorPositions,
    double subjectHeight,
    double initialSubjectDist) const {

    QVector<QVector3D> rawPts;
    for (const auto& p : rig.points) rawPts.append(p.position);
    SplineLut lut(rawPts);

    const double dur = std::max(0.01, rig.pathEndS - rig.pathStartS);
    const double normT = std::clamp((timeS - rig.pathStartS) / dur, 0.0, 1.0);
    const double easedDistRatio = evaluateEase(normT, rig.ease);
    const double targetDist = lut.totalLength() * easedDistRatio;

    const SplineSegmentResult segRes = lut.sampleAtDistance(targetDist);
    QVector3D camPos = segRes.position;
    const QVector3D tangent = segRes.tangent;

    double horizAp = 36.0, vertAp = 20.25;
    sensorDimensions(rig.sensorPreset, horizAp, vertAp);

    double focal = rig.focalStart + (rig.focalEnd - rig.focalStart) * easedDistRatio;

    // Resolve target
    QVector3D targetPos = rig.lookAtPoint;
    QString targetKey;
    if (!rig.aimTargets.isEmpty()) {
        targetKey = rig.aimTargets.first();
    } else if (!rig.shotSubject.isEmpty()) {
        targetKey = rig.shotSubject;
    }

    if (!targetKey.isEmpty()) {
        if (actorPositions.contains(targetKey)) {
            targetPos = actorPositions.value(targetKey);
        } else if (actorPositions.contains("/World/" + targetKey)) {
            targetPos = actorPositions.value("/World/" + targetKey);
        }
    }

    const QVector3D resolvedTarget = targetPos + rig.aimOffset;
    const double curDistToTarget = static_cast<double>((camPos - resolvedTarget).length());

    if (rig.dollyZoomEnabled && initialSubjectDist > 1e-3) {
        focal = std::clamp(rig.focalStart * (curDistToTarget / initialSubjectDist), 12.0, 200.0);
    }

    double rollDeg = 0.0;
    if (!rig.points.isEmpty()) {
        const double r0 = rig.points.first().rollDeg;
        const double r1 = rig.points.last().rollDeg;
        rollDeg = r0 + (r1 - r0) * easedDistRatio;
    }

    QVector3D rot;
    if (rig.aimMode == CameraAimMode::LookAt || rig.aimMode == CameraAimMode::Frame) {
        rot = computeLookAtRotation(camPos, resolvedTarget, rollDeg);
    } else if (rig.aimMode == CameraAimMode::PathTangent) {
        rot = computePathTangentRotation(tangent, rollDeg);
    } else {
        rot = computePathTangentRotation(tangent, rollDeg);
    }

    if (rig.shakeAmp > 1e-4) {
        QVector3D posShk, rotShk;
        evaluateSeededShakeCpp(timeS, 42, rig.shakeAmp, rig.shakeFreq, posShk, rotShk);
        camPos += posShk;
        rot += rotShk;
    }

    double focusDist = rig.focusDistance;
    if (rig.dofEnabled) {
        focusDist = std::max(0.1, curDistToTarget);
    }

    CameraSample sample;
    sample.timeS = timeS;
    sample.position = camPos;
    sample.rotationDeg = rot;
    sample.focalLength = focal;
    sample.horizontalAperture = horizAp;
    sample.verticalAperture = vertAp;
    sample.focusDistance = focusDist;
    sample.fStop = rig.fStop;
    return sample;
}

QVector<CameraSample> CameraRigEvaluator::evaluateSequence(
    const CameraRigData& rig,
    double startS,
    double endS,
    const QHash<QString, QVector<QPair<double, QVector3D>>>& actorPositionsOverTime,
    double subjectHeight) const {

    QVector<CameraSample> samples;
    const int nFrames = std::max(1, static_cast<int>(std::round((endS - startS) * myFps)) + 1);

    double initDist = -1.0;
    if (!rig.points.isEmpty()) {
        if (!rig.aimTargets.isEmpty() || !rig.shotSubject.isEmpty()) {
            const QString tgt = !rig.aimTargets.isEmpty() ? rig.aimTargets.first() : rig.shotSubject;
            if (actorPositionsOverTime.contains(tgt) && !actorPositionsOverTime[tgt].isEmpty()) {
                initDist = static_cast<double>((rig.points.first().position - actorPositionsOverTime[tgt].first().second).length());
            }
        } else {
            initDist = static_cast<double>((rig.points.first().position - (rig.lookAtPoint + rig.aimOffset)).length());
        }
    }

    for (int i = 0; i < nFrames; ++i) {
        const double t = startS + static_cast<double>(i) / myFps;
        QHash<QString, QVector3D> curActorPos;
        for (auto it = actorPositionsOverTime.constBegin(); it != actorPositionsOverTime.constEnd(); ++it) {
            const auto& series = it.value();
            if (series.isEmpty()) continue;
            double bestDt = 1e9;
            QVector3D bestPos = series.first().second;
            for (const auto& pair : series) {
                const double dt = std::abs(pair.first - t);
                if (dt < bestDt) {
                    bestDt = dt;
                    bestPos = pair.second;
                }
            }
            curActorPos.insert(it.key(), bestPos);
        }

        samples.append(evaluate(rig, t, curActorPos, subjectHeight, initDist));
    }
    return samples;
}

QString CameraRigEvaluator::shotSizeToString(CameraShotSize size) {
    switch (size) {
        case CameraShotSize::ExtremeWide: return QStringLiteral("extreme_wide");
        case CameraShotSize::Wide: return QStringLiteral("wide");
        case CameraShotSize::Medium: return QStringLiteral("medium");
        case CameraShotSize::MediumCloseUp: return QStringLiteral("medium_close_up");
        case CameraShotSize::CloseUp: return QStringLiteral("close_up");
        case CameraShotSize::ExtremeCloseUp: return QStringLiteral("extreme_close_up");
        case CameraShotSize::OverTheShoulder: return QStringLiteral("over_the_shoulder");
        case CameraShotSize::TopDown: return QStringLiteral("top_down");
    }
    return QStringLiteral("medium");
}

CameraShotSize CameraRigEvaluator::stringToShotSize(const QString& str) {
    const QString s = str.toLower().trimmed().replace('-', '_').replace(' ', '_');
    if (s == "extreme_wide") return CameraShotSize::ExtremeWide;
    if (s == "wide") return CameraShotSize::Wide;
    if (s == "medium") return CameraShotSize::Medium;
    if (s == "medium_close_up") return CameraShotSize::MediumCloseUp;
    if (s == "close_up") return CameraShotSize::CloseUp;
    if (s == "extreme_close_up") return CameraShotSize::ExtremeCloseUp;
    if (s == "over_the_shoulder") return CameraShotSize::OverTheShoulder;
    if (s == "top_down") return CameraShotSize::TopDown;
    return CameraShotSize::Medium;
}

QString CameraRigEvaluator::aimModeToString(CameraAimMode mode) {
    switch (mode) {
        case CameraAimMode::Free: return QStringLiteral("free");
        case CameraAimMode::LookAt: return QStringLiteral("look_at");
        case CameraAimMode::Frame: return QStringLiteral("frame");
        case CameraAimMode::PathTangent: return QStringLiteral("path_tangent");
    }
    return QStringLiteral("look_at");
}

CameraAimMode CameraRigEvaluator::stringToAimMode(const QString& str) {
    const QString s = str.toLower().trimmed().replace('-', '_').replace(' ', '_');
    if (s == "free") return CameraAimMode::Free;
    if (s == "look_at" || s == "lookat") return CameraAimMode::LookAt;
    if (s == "frame") return CameraAimMode::Frame;
    if (s == "path_tangent" || s == "pathtangent" || s == "tangent") return CameraAimMode::PathTangent;
    return CameraAimMode::LookAt;
}

QString CameraRigEvaluator::easeToString(CameraEaseType ease) {
    switch (ease) {
        case CameraEaseType::Linear: return QStringLiteral("linear");
        case CameraEaseType::EaseIn: return QStringLiteral("ease_in");
        case CameraEaseType::EaseOut: return QStringLiteral("ease_out");
        case CameraEaseType::EaseInOut: return QStringLiteral("ease_in_out");
    }
    return QStringLiteral("ease_in_out");
}

CameraEaseType CameraRigEvaluator::stringToEase(const QString& str) {
    const QString s = str.toLower().trimmed().replace('-', '_').replace(' ', '_');
    if (s == "linear" || s == "constant" || s == "none") return CameraEaseType::Linear;
    if (s == "ease_in" || s == "easein") return CameraEaseType::EaseIn;
    if (s == "ease_out" || s == "easeout") return CameraEaseType::EaseOut;
    return CameraEaseType::EaseInOut;
}

QString CameraRigEvaluator::sensorPresetToString(CameraSensorPreset preset) {
    switch (preset) {
        case CameraSensorPreset::Super35: return QStringLiteral("super35");
        case CameraSensorPreset::FullFrame: return QStringLiteral("full_frame");
        case CameraSensorPreset::Standard16_9: default: return QStringLiteral("16_9");
    }
}

CameraSensorPreset CameraRigEvaluator::stringToSensorPreset(const QString& str) {
    const QString s = str.toLower().trimmed().replace('-', '_').replace(' ', '_');
    if (s == "super35") return CameraSensorPreset::Super35;
    if (s == "full_frame" || s == "fullframe") return CameraSensorPreset::FullFrame;
    return CameraSensorPreset::Standard16_9;
}

// -----------------------------------------------------------------------------
// USD Stage Authoring & Baking
// -----------------------------------------------------------------------------

bool saveCameraRigToStage(UsdStageRefPtr stage, const CameraRigData& rig, double startFrame, double endFrame, double fps) {
    if (!stage) return false;

    const SdfPath rigPath("/World/CameraRig/Rig_01");
    UsdGeomXform rigXform = UsdGeomXform::Define(stage, rigPath);
    UsdPrim rigPrim = rigXform.GetPrim();
    rigPrim.SetCustomDataByKey(TfToken("schemaVersion"), VtValue(rig.schemaVersion));
    rigPrim.CreateAttribute(TfToken("ps:path_drawn"), SdfValueTypeNames->Bool).Set(rig.pathDrawn);

    rigPrim.CreateAttribute(TfToken("ps:path_start_s"), SdfValueTypeNames->Float).Set(static_cast<float>(rig.pathStartS));
    rigPrim.CreateAttribute(TfToken("ps:path_end_s"), SdfValueTypeNames->Float).Set(static_cast<float>(rig.pathEndS));
    rigPrim.CreateAttribute(TfToken("ps:ease"), SdfValueTypeNames->Token).Set(TfToken(CameraRigEvaluator::easeToString(rig.ease).toStdString()));
    rigPrim.CreateAttribute(TfToken("ps:aim_mode"), SdfValueTypeNames->Token).Set(TfToken(CameraRigEvaluator::aimModeToString(rig.aimMode).toStdString()));
    rigPrim.CreateAttribute(TfToken("ps:aim_point"), SdfValueTypeNames->Float3).Set(GfVec3f(rig.lookAtPoint.x(), rig.lookAtPoint.y(), rig.lookAtPoint.z()));
    rigPrim.CreateAttribute(TfToken("ps:aim_offset"), SdfValueTypeNames->Float3).Set(GfVec3f(rig.aimOffset.x(), rig.aimOffset.y(), rig.aimOffset.z()));
    rigPrim.CreateAttribute(TfToken("ps:aim_smoothing"), SdfValueTypeNames->Float).Set(static_cast<float>(rig.aimSmoothing));
    rigPrim.CreateAttribute(TfToken("ps:shot_size"), SdfValueTypeNames->Token).Set(TfToken(CameraRigEvaluator::shotSizeToString(rig.shotSize).toStdString()));
    rigPrim.CreateAttribute(TfToken("ps:focal_start"), SdfValueTypeNames->Float).Set(static_cast<float>(rig.focalStart));
    rigPrim.CreateAttribute(TfToken("ps:focal_end"), SdfValueTypeNames->Float).Set(static_cast<float>(rig.focalEnd));
    rigPrim.CreateAttribute(TfToken("ps:sensor_preset"), SdfValueTypeNames->Token).Set(TfToken(CameraRigEvaluator::sensorPresetToString(rig.sensorPreset).toStdString()));
    rigPrim.CreateAttribute(TfToken("ps:aspect_ratio"), SdfValueTypeNames->Token).Set(TfToken(rig.aspectRatio.toStdString()));

    if (!rig.aimTargets.isEmpty()) {
        UsdRelationship rel = rigPrim.CreateRelationship(TfToken("ps:aim_targets"));
        SdfPathVector targets;
        for (const QString& t : rig.aimTargets) targets.push_back(SdfPath(t.toStdString()));
        rel.SetTargets(targets);
    }

    // BasisCurves
    const SdfPath curvePath = rigPath.AppendChild(TfToken("PositionPath"));
    UsdGeomBasisCurves curves = UsdGeomBasisCurves::Define(stage, curvePath);
    curves.GetTypeAttr().Set(UsdGeomTokens->cubic);
    curves.GetBasisAttr().Set(TfToken("catmullRom"));

    VtArray<GfVec3f> pts;
    for (const auto& p : rig.points) {
        pts.push_back(GfVec3f(p.position.x(), p.position.y(), p.position.z()));
    }
    curves.GetPointsAttr().Set(pts);
    VtArray<int> counts;
    counts.push_back(pts.size());
    curves.GetCurveVertexCountsAttr().Set(counts);

    return true;
}

bool loadCameraRigFromStage(UsdStageRefPtr stage, CameraRigData& outRig) {
    if (!stage) return false;
    const SdfPath rigPath("/World/CameraRig/Rig_01");
    UsdPrim prim = stage->GetPrimAtPath(rigPath);
    if (!prim || !prim.IsValid()) return false;

    outRig = CameraRigData();
    UsdAttribute pathDrawnAttr = prim.GetAttribute(TfToken("ps:path_drawn"));
    if (pathDrawnAttr) pathDrawnAttr.Get(&outRig.pathDrawn);
    if (prim.HasCustomDataKey(TfToken("schemaVersion"))) {
        outRig.schemaVersion = prim.GetCustomDataByKey(TfToken("schemaVersion")).Get<int>();
    }

    auto getFloat = [&](const char* name, double def) -> double {
        UsdAttribute attr = prim.GetAttribute(TfToken(name));
        if (attr && attr.IsValid() && attr.HasAuthoredValue()) {
            float v = 0.0f;
            attr.Get(&v);
            return static_cast<double>(v);
        }
        return def;
    };
    auto getToken = [&](const char* name, const QString& def) -> QString {
        UsdAttribute attr = prim.GetAttribute(TfToken(name));
        if (attr && attr.IsValid() && attr.HasAuthoredValue()) {
            TfToken tok;
            attr.Get(&tok);
            return QString::fromStdString(tok.GetString());
        }
        return def;
    };

    outRig.pathStartS = getFloat("ps:path_start_s", 0.0);
    outRig.pathEndS = getFloat("ps:path_end_s", 5.0);
    outRig.ease = CameraRigEvaluator::stringToEase(getToken("ps:ease", "ease_in_out"));
    outRig.aimMode = CameraRigEvaluator::stringToAimMode(getToken("ps:aim_mode", "look_at"));
    outRig.aimSmoothing = getFloat("ps:aim_smoothing", 0.2);
    outRig.shotSize = CameraRigEvaluator::stringToShotSize(getToken("ps:shot_size", "medium"));
    outRig.focalStart = getFloat("ps:focal_start", 35.0);
    outRig.focalEnd = getFloat("ps:focal_end", 35.0);
    outRig.sensorPreset = CameraRigEvaluator::stringToSensorPreset(getToken("ps:sensor_preset", "16_9"));
    outRig.aspectRatio = getToken("ps:aspect_ratio", "16:9");

    UsdAttribute aimPointAttr = prim.GetAttribute(TfToken("ps:aim_point"));
    if (aimPointAttr && aimPointAttr.IsValid() && aimPointAttr.HasAuthoredValue()) {
        GfVec3f ap(0.0f);
        aimPointAttr.Get(&ap);
        outRig.lookAtPoint = QVector3D(ap[0], ap[1], ap[2]);
        outRig.hasCustomLookAt = true;
    }

    UsdAttribute offsetAttr = prim.GetAttribute(TfToken("ps:aim_offset"));
    if (offsetAttr && offsetAttr.IsValid() && offsetAttr.HasAuthoredValue()) {
        GfVec3f off(0.0f);
        offsetAttr.Get(&off);
        outRig.aimOffset = QVector3D(off[0], off[1], off[2]);
    }

    UsdRelationship rel = prim.GetRelationship(TfToken("ps:aim_targets"));
    if (rel && rel.IsValid()) {
        SdfPathVector tgts;
        rel.GetTargets(&tgts);
        for (const auto& p : tgts) outRig.aimTargets.append(QString::fromStdString(p.GetString()));
    }

    const SdfPath curvePath = rigPath.AppendChild(TfToken("PositionPath"));
    UsdPrim curvePrim = stage->GetPrimAtPath(curvePath);
    if (curvePrim && curvePrim.IsValid()) {
        UsdGeomBasisCurves curves(curvePrim);
        VtArray<GfVec3f> pts;
        curves.GetPointsAttr().Get(&pts);
        outRig.points.clear();
        for (const auto& pt : pts) {
            CameraRigPoint rp;
            rp.position = QVector3D(pt[0], pt[1], pt[2]);
            outRig.points.append(rp);
        }
    }
    return true;
}

bool bakeCameraRigToShotCam(UsdStageRefPtr stage, const CameraRigData& rig, double startFrame, double endFrame, double fps,
                            const QHash<QString, QVector<QPair<double, QVector3D>>>& actorPositionsOverTime, const QString& cameraPath) {
    if (!stage) return false;

    CameraRigEvaluator evaluator(fps);
    const double startS = startFrame / fps;
    const double endS = endFrame / fps;
    const QVector<CameraSample> keyframes = evaluator.evaluateSequence(rig, startS, endS, actorPositionsOverTime);

    saveCameraRigToStage(stage, rig, startFrame, endFrame, fps);

    const SdfPath camPath(cameraPath.toStdString());
    UsdPrim camPrim = stage->GetPrimAtPath(camPath);
    if (!camPrim || !camPrim.IsA<UsdGeomCamera>()) {
        UsdGeomCamera geom = UsdGeomCamera::Define(stage, camPath);
        camPrim = geom.GetPrim();
    }

    camPrim.SetCustomDataByKey(TfToken("painterspace_mode"), VtValue(std::string("manual")));
    camPrim.SetCustomDataByKey(TfToken("painterspace_rig"), VtValue(std::string("/World/CameraRig/Rig_01")));
    camPrim.SetCustomDataByKey(TfToken("painterspace_baked"), VtValue(true));

    UsdGeomCamera camGeom(camPrim);
    UsdGeomXformable camXf(camPrim);

    UsdGeomXformOp translateOp;
    UsdGeomXformOp rotateOp;
    bool resetsXform = false;
    for (const auto& op : camXf.GetOrderedXformOps(&resetsXform)) {
        if (op.GetOpType() == UsdGeomXformOp::TypeTranslate) translateOp = op;
        else if (op.GetOpType() == UsdGeomXformOp::TypeRotateXYZ) rotateOp = op;
    }
    if (!translateOp) translateOp = camXf.AddTranslateOp();
    if (!rotateOp) rotateOp = camXf.AddRotateXYZOp();

    std::vector<UsdGeomXformOp> orderOps = {translateOp, rotateOp};
    camXf.SetXformOpOrder(orderOps);

    double horizAp = 36.0, vertAp = 20.25;
    CameraRigEvaluator::sensorDimensions(rig.sensorPreset, horizAp, vertAp);
    camGeom.GetHorizontalApertureAttr().Set(static_cast<float>(horizAp));
    camGeom.GetVerticalApertureAttr().Set(static_cast<float>(vertAp));
    camGeom.GetClippingRangeAttr().Set(GfVec2f(0.1f, 1000.0f));

    QVector3D prevRot(0.0f, 0.0f, 0.0f);
    bool hasPrev = false;

    for (const auto& kf : keyframes) {
        const int frameNum = static_cast<int>(std::round(kf.timeS * fps));
        const UsdTimeCode tc(frameNum);

        translateOp.Set(GfVec3d(kf.position.x(), kf.position.y(), kf.position.z()), tc);

        QVector3D r = kf.rotationDeg;
        if (hasPrev) {
            float dPitch = r.x() - prevRot.x();
            while (dPitch > 180.0f) { r.setX(r.x() - 360.0f); dPitch = r.x() - prevRot.x(); }
            while (dPitch < -180.0f) { r.setX(r.x() + 360.0f); dPitch = r.x() - prevRot.x(); }

            float dYaw = r.y() - prevRot.y();
            while (dYaw > 180.0f) { r.setY(r.y() - 360.0f); dYaw = r.y() - prevRot.y(); }
            while (dYaw < -180.0f) { r.setY(r.y() + 360.0f); dYaw = r.y() - prevRot.y(); }

            float dRoll = r.z() - prevRot.z();
            while (dRoll > 180.0f) { r.setZ(r.z() - 360.0f); dRoll = r.z() - prevRot.z(); }
            while (dRoll < -180.0f) { r.setZ(r.z() + 360.0f); dRoll = r.z() - prevRot.z(); }
        }
        prevRot = r;
        hasPrev = true;

        rotateOp.Set(GfVec3f(r.x(), r.y(), r.z()), tc);
        camGeom.GetFocalLengthAttr().Set(static_cast<float>(kf.focalLength), tc);
    }

    if (!keyframes.isEmpty()) {
        const auto& firstKf = keyframes.first();
        translateOp.Set(GfVec3d(firstKf.position.x(), firstKf.position.y(), firstKf.position.z()), UsdTimeCode::Default());
        rotateOp.Set(GfVec3f(firstKf.rotationDeg.x(), firstKf.rotationDeg.y(), firstKf.rotationDeg.z()), UsdTimeCode::Default());
        camGeom.GetFocalLengthAttr().Set(static_cast<float>(firstKf.focalLength), UsdTimeCode::Default());
    }
    return true;
}

bool resetCameraRigOnStage(UsdStageRefPtr stage) {
    if (!stage) return false;
    const SdfPath rigScope("/World/CameraRig");
    if (stage->GetPrimAtPath(rigScope)) {
        stage->RemovePrim(rigScope);
    }
    const SdfPath camPath("/World/ShotCam");
    UsdPrim camPrim = stage->GetPrimAtPath(camPath);
    if (camPrim && camPrim.IsValid()) {
        camPrim.ClearCustomDataByKey(TfToken("painterspace_mode"));
        camPrim.ClearCustomDataByKey(TfToken("painterspace_rig"));
        camPrim.ClearCustomDataByKey(TfToken("painterspace_baked"));
        stage->RemovePrim(camPath);
    }
    return true;
}

bool convertShotCamToRig(UsdStageRefPtr stage, double startFrame, double endFrame, double fps, CameraRigData& outRig) {
    if (!stage) return false;
    const SdfPath camPath("/World/ShotCam");
    UsdPrim camPrim = stage->GetPrimAtPath(camPath);
    if (!camPrim || !camPrim.IsA<UsdGeomCamera>()) return false;

    UsdGeomCamera camGeom(camPrim);
    UsdGeomXformable camXf(camPrim);

    std::vector<double> timeSamples;
    camGeom.GetFocalLengthAttr().GetTimeSamples(&timeSamples);
    if (timeSamples.empty()) {
        for (double f = startFrame; f <= endFrame; f += 1.0) timeSamples.push_back(f);
    }

    QVector<QVector3D> rawPositions;
    for (double tcVal : timeSamples) {
        GfMatrix4d mat = camXf.ComputeLocalToWorldTransform(UsdTimeCode(tcVal));
        GfVec3d tr = mat.ExtractTranslation();
        rawPositions.append(QVector3D(static_cast<float>(tr[0]), static_cast<float>(tr[1]), static_cast<float>(tr[2])));
    }

    QVector<QVector3D> simplified = simplifyPointsRdp(rawPositions, 0.05);
    outRig = CameraRigData();
    for (const auto& pos : simplified) {
        CameraRigPoint p;
        p.position = pos;
        outRig.points.append(p);
    }
    outRig.pathStartS = timeSamples.front() / fps;
    outRig.pathEndS = timeSamples.back() / fps;
    if (outRig.pathEndS <= outRig.pathStartS) {
        outRig.pathEndS = outRig.pathStartS + (endFrame > startFrame ? (endFrame - startFrame) / fps : 5.0);
    }
    outRig.aimMode = CameraAimMode::LookAt;

    if (!timeSamples.empty()) {
        GfMatrix4d mat = camXf.ComputeLocalToWorldTransform(UsdTimeCode(timeSamples.front()));
        GfVec3d camPos = mat.ExtractTranslation();
        GfVec3d fwd = mat.TransformDir(GfVec3d(0.0, 0.0, -1.0));
        fwd.Normalize();
        GfVec3d targetPt = camPos + fwd * 4.0;
        outRig.lookAtPoint = QVector3D(static_cast<float>(targetPt[0]), static_cast<float>(targetPt[1]), static_cast<float>(targetPt[2]));
        outRig.hasCustomLookAt = true;
    }

    float fStart = 35.0f, fEnd = 35.0f;
    camGeom.GetFocalLengthAttr().Get(&fStart, UsdTimeCode(timeSamples.front()));
    camGeom.GetFocalLengthAttr().Get(&fEnd, UsdTimeCode(timeSamples.back()));
    outRig.focalStart = fStart;
    outRig.focalEnd = fEnd;
    return true;
}

CameraRigData createCameraRigPreset(const QString& presetName, const QVector3D& targetPos, double distance, double durationS, CameraShotSize shotSize) {
    const QString p = presetName.toLower().trimmed().replace('-', '_').replace(' ', '_');
    CameraRigData rig;
    rig.shotSize = shotSize;
    rig.pathStartS = 0.0;
    rig.pathEndS = std::max(0.5, durationS);
    rig.aimMode = CameraAimMode::LookAt;
    rig.lookAtPoint = targetPos;
    rig.hasCustomLookAt = true;
    rig.aimOffset = QVector3D(0.0f, 0.2f, 0.0f);

    const float d = static_cast<float>(distance);

    if (p == "dolly_in") {
        rig.points.append({targetPos + QVector3D(0.0f, 0.3f, d)});
        rig.points.append({targetPos + QVector3D(0.0f, 0.2f, d * 0.45f)});
    } else if (p == "dolly_out") {
        rig.points.append({targetPos + QVector3D(0.0f, 0.2f, d * 0.5f)});
        rig.points.append({targetPos + QVector3D(0.0f, 0.4f, d * 1.5f)});
    } else if (p == "truck_left") {
        rig.points.append({targetPos + QVector3D(d * 0.6f, 0.3f, d)});
        rig.points.append({targetPos + QVector3D(-d * 0.6f, 0.3f, d)});
    } else if (p == "truck_right") {
        rig.points.append({targetPos + QVector3D(-d * 0.6f, 0.3f, d)});
        rig.points.append({targetPos + QVector3D(d * 0.6f, 0.3f, d)});
    } else if (p == "pedestal_up") {
        rig.points.append({targetPos + QVector3D(0.0f, -0.4f, d)});
        rig.points.append({targetPos + QVector3D(0.0f, 1.8f, d)});
    } else if (p == "pedestal_down") {
        rig.points.append({targetPos + QVector3D(0.0f, 1.8f, d)});
        rig.points.append({targetPos + QVector3D(0.0f, -0.4f, d)});
    } else if (p == "crane_arc") {
        rig.points.append({targetPos + QVector3D(d * 0.5f, -0.3f, d)});
        rig.points.append({targetPos + QVector3D(0.0f, 1.2f, d * 0.85f)});
        rig.points.append({targetPos + QVector3D(-d * 0.4f, 2.5f, d * 0.7f)});
    } else if (p == "orbit") {
        const double angles[4] = {-0.6, -0.2, 0.2, 0.6};
        for (double ang : angles) {
            const float x = targetPos.x() + static_cast<float>(distance * std::sin(ang));
            const float z = targetPos.z() + static_cast<float>(distance * std::cos(ang));
            rig.points.append({QVector3D(x, targetPos.y() + 0.3f, z)});
        }
    } else {
        rig.points.append({targetPos + QVector3D(0.0f, 0.3f, d)});
        rig.points.append({targetPos + QVector3D(0.0f, 0.3f, d * 0.85f)});
    }
    return rig;
}

QVector<QVector3D> simplifyPointsRdp(const QVector<QVector3D>& points, double epsilon) {
    if (points.size() <= 2) return points;

    const QVector3D pFirst = points.first();
    const QVector3D pLast = points.last();
    const QVector3D segVec = pLast - pFirst;
    const double segLen = static_cast<double>(segVec.length());

    double maxDist = 0.0;
    int index = 0;

    for (int i = 1; i < points.size() - 1; ++i) {
        const QVector3D pt = points[i];
        double dist = 0.0;
        if (segLen < 1e-6) {
            dist = static_cast<double>((pt - pFirst).length());
        } else {
            const QVector3D cross = QVector3D::crossProduct(pt - pFirst, segVec);
            dist = static_cast<double>(cross.length()) / segLen;
        }
        if (dist > maxDist) {
            maxDist = dist;
            index = i;
        }
    }

    if (maxDist > epsilon) {
        QVector<QVector3D> left = simplifyPointsRdp(points.mid(0, index + 1), epsilon);
        QVector<QVector3D> right = simplifyPointsRdp(points.mid(index), epsilon);
        left.removeLast();
        left.append(right);
        return left;
    } else {
        return {pFirst, pLast};
    }
}

QJsonObject cameraRigToJson(const CameraRigData& rig, const QJsonObject& previous) {
    auto camera = previous;
    auto path = camera["path"].toObject();
    const auto old = path["waypoints"].toArray();
    QJsonArray waypoints;
    for (int i = 0; i < rig.points.size(); ++i) {
        auto w = i < old.size() ? old[i].toObject() : QJsonObject();
        const auto& point = rig.points[i];
        w["p"] = QJsonArray{point.position.x(), point.position.y(), point.position.z()};
        if (point.timeS >= 0) w["t"] = point.timeS;
        if (point.focalLength > 0) w["focal_mm"] = point.focalLength;
        if (!w.contains("ease")) w["ease"] = "constant";
        if (!w.contains("dwell_s")) w["dwell_s"] = 0;
        waypoints.append(w);
    }
    path["waypoints"] = waypoints;
    path["user_drawn"] = rig.pathDrawn;
    path["time_range"] = QJsonArray{rig.pathStartS, rig.pathEndS};
    auto speed = path["speed"].toObject();
    speed["profile"] = rig.ease == CameraEaseType::Linear ? "constant" : CameraRigEvaluator::easeToString(rig.ease);
    path["speed"] = speed;
    camera["path"] = path;
    auto lens = camera["lens"].toObject(); lens["focal_mm"] = rig.focalStart;
    lens["dof_enabled"] = rig.dofEnabled; lens["fstop"] = rig.fStop; lens["dolly_zoom"] = rig.dollyZoomEnabled;
    if (lens["keys"].toArray().isEmpty() && std::abs(rig.focalEnd - rig.focalStart) > .001)
        lens["keys"] = QJsonArray{QJsonObject{{"t", rig.pathEndS}, {"focal_mm", rig.focalEnd}}};
    double horizontal = 36., vertical = 20.25;
    CameraRigEvaluator::sensorDimensions(rig.sensorPreset,horizontal,vertical);
    const auto ratio = rig.aspectRatio.split(':');
    if (ratio.size()==2 && ratio[0].toDouble()>0 && ratio[1].toDouble()>0) vertical=horizontal*ratio[1].toDouble()/ratio[0].toDouble();
    lens["horizontal_aperture_mm"] = horizontal; lens["vertical_aperture_mm"] = vertical;
    camera["aspect_ratio"] = rig.aspectRatio;
    camera["sensor_preset"] = CameraRigEvaluator::sensorPresetToString(rig.sensorPreset);
    camera["lens"] = lens;
    auto aim = camera["aim"].toObject();
    auto segments = aim["segments"].toArray();
    auto segment = segments.isEmpty() ? QJsonObject() : segments[0].toObject();
    QString mode = "look_at";
    if (rig.aimMode == CameraAimMode::PathTangent) mode = "along_path";
    if (rig.aimMode == CameraAimMode::Free) mode = "free";
    segment["mode"] = mode;
    if (!segment.contains("t")) segment["t"] = QJsonArray{rig.pathStartS, rig.pathEndS};
    QJsonArray targets;
    for (const auto& target : rig.aimTargets) {
        QJsonObject spec{{"actor", target}};
        if (!rig.aimBone.isEmpty()) spec["joint"] = rig.aimBone;
        spec["offset"] = QJsonArray{rig.aimOffset.x(), rig.aimOffset.y(), rig.aimOffset.z()};
        targets.append(spec);
    }
    segment["targets"] = targets;
    if (rig.hasCustomLookAt && targets.isEmpty())
        segment["point"] = QJsonArray{rig.lookAtPoint.x(), rig.lookAtPoint.y(), rig.lookAtPoint.z()};
    else segment.remove("point");
    if (segments.isEmpty()) segments.append(segment); else segments[0] = segment;
    aim["segments"] = segments; aim["smoothing_s"] = rig.aimSmoothing;
    camera["aim"] = aim;
    auto shake = camera["shake"].toObject(); shake["amount"] = rig.shakeAmp;
    if (!shake.contains("seed")) shake["seed"] = 7;
    camera["shake"] = shake;
    return camera;
}

CameraRigData cameraRigFromJson(const QJsonObject& camera) {
    CameraRigData rig;
    auto vector = [](const QJsonValue& value) { auto a = value.toArray();
        return a.size() == 3 ? QVector3D(a[0].toDouble(), a[1].toDouble(), a[2].toDouble()) : QVector3D(); };
    rig.aspectRatio = camera["aspect_ratio"].toString("16:9");
    rig.sensorPreset = CameraRigEvaluator::stringToSensorPreset(camera["sensor_preset"].toString("16_9"));
    const auto path = camera["path"].toObject();
    rig.pathDrawn = path["user_drawn"].toBool(false);
    for (const auto& value : path["waypoints"].toArray()) {
        auto w = value.toObject(); CameraRigPoint p;
        p.position = vector(w["p"]); p.timeS = w["t"].toDouble(-1);
        p.focalLength = w["focal_mm"].toDouble(-1); rig.points.append(p);
    }
    auto range = path["time_range"].toArray();
    if (range.size() == 2) { rig.pathStartS = range[0].toDouble(); rig.pathEndS = range[1].toDouble(); }
    rig.ease = CameraRigEvaluator::stringToEase(path["speed"].toObject()["profile"].toString("linear"));
    auto lens = camera["lens"].toObject(); rig.focalStart = lens["focal_mm"].toDouble(35); rig.focalEnd = rig.focalStart;
    auto keys = lens["keys"].toArray(); if (!keys.isEmpty()) rig.focalEnd = keys.last().toObject()["focal_mm"].toDouble(rig.focalStart);
    rig.dollyZoomEnabled = lens["dolly_zoom"].toBool();
    rig.dofEnabled = lens["dof_enabled"].toBool(); rig.fStop = lens["fstop"].toDouble(2.8);
    const auto aim = camera["aim"].toObject(); rig.aimSmoothing = aim["smoothing_s"].toDouble(.35);
    auto segments = aim["segments"].toArray();
    if (!segments.isEmpty()) {
        auto seg = segments[0].toObject(); const QString mode = seg["mode"].toString();
        rig.aimMode = mode == "along_path" ? CameraAimMode::PathTangent : mode == "free" || mode == "lock_direction" ? CameraAimMode::Free : CameraAimMode::LookAt;
        for (const auto& target : seg["targets"].toArray()) {
            auto spec = target.toObject(); rig.aimTargets.append(spec["actor"].toString());
            rig.aimBone = spec["joint"].toString(); rig.aimOffset = vector(spec["offset"]);
        }
        if (seg.contains("point")) { rig.lookAtPoint = vector(seg["point"]); rig.hasCustomLookAt = true; }
    }
    rig.shakeAmp = camera["shake"].toObject()["amount"].toDouble();
    return rig;
}
