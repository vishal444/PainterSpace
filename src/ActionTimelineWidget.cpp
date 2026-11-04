#include "ActionTimelineWidget.h"

#include <QComboBox>
#include <QDateTime>
#include <QCheckBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDoubleSpinBox>
#include <QHBoxLayout>
#include <QFormLayout>
#include <QHeaderView>
#include <QKeyEvent>
#include <QLabel>
#include <QLineEdit>
#include <QMenu>
#include <QMouseEvent>
#include <QPainter>
#include <QPainterPath>
#include <QPushButton>
#include <QScrollArea>
#include <QSet>
#include <QToolTip>
#include <QVBoxLayout>
#include <algorithm>
#include <cmath>

static const int kRulerHeight = 22;
static const int kTrackHeight = 48;
static const int kHeaderWidth = 114;

static double nominalMovementSpeed(const QString& action) {
    if (action == "walk" || action == "walk_to") return 1.4;
    if (action == "run" || action == "run_to" || action == "flee_from") return 4.2;
    if (action == "jog" || action == "jog_to") return 2.6;
    if (action == "sprint" || action == "sprint_to") return 6.0;
    return 0.0;
}

// -------------------------------------------------------------
// JSON Serialization
// -------------------------------------------------------------
QJsonObject ActionSegment::toJson() const {
    QJsonObject obj;
    obj["id"] = id;
    obj["actor_path"] = actorPath;
    obj["actor_label"] = actorLabel;
    obj["start_s"] = startS;
    obj["end_s"] = endS;
    obj["action"] = action;
    if (generated) obj["generated"] = true;
    if (!sourceBeatId.isEmpty()) obj["source_beat_id"] = sourceBeatId;
    if (!activity.isEmpty()) obj["activity"] = activity;
    if (!groupId.isEmpty()) {
        obj["group_id"] = groupId;
        obj["participants"] = QJsonArray::fromStringList(participants);
    }
    obj["lock"] = lock;
    obj["target"] = target;
    obj["path"] = pathId;
    obj["note"] = note;
    obj["speed_profile"] = speedProfile;
    if (distanceM >= 0.0) obj["distance_m"] = distanceM;
    obj["auto_timing"] = autoTiming;
    obj["flexible"] = flexible;
    obj["tolerance_s"] = toleranceS;
    obj["color"] = color.name();
    return obj;
}

ActionSegment ActionSegment::fromJson(const QJsonObject& obj) {
    ActionSegment seg;
    seg.id = obj.value("id").toString();
    seg.actorPath = obj.value("actor_path").toString();
    seg.actorLabel = obj.value("actor_label").toString();
    seg.startS = obj.value("start_s").toDouble(0.0);
    seg.endS = obj.value("end_s").toDouble(5.0);
    seg.action = obj.value("action").toString("walk_to");
    seg.generated = obj.value("generated").toBool(false);
    seg.sourceBeatId = obj.value("source_beat_id").toString();
    seg.activity = obj.value("activity").toString();
    seg.groupId = obj.value("group_id").toString();
    for (const auto& participant : obj.value("participants").toArray())
        seg.participants.append(participant.toString());
    seg.lock = obj.value("lock").toString("locked");
    seg.target = obj.value("target").toString();
    seg.pathId = obj.value("path").toString();
    seg.note = obj.value("note").toString();
    seg.speedProfile = obj.value("speed_profile").toString("constant");
    seg.distanceM = obj.value("distance_m").toDouble(-1.0);
    seg.autoTiming = obj.value("auto_timing").toBool(false);
    seg.flexible = obj.value("flexible").toBool();
    seg.toleranceS = obj.value("tolerance_s").toDouble();
    if (obj.contains("color")) {
        seg.color = QColor(obj.value("color").toString("#58a6ff"));
    } else if (!seg.groupId.isEmpty()) {
        seg.color = QColor("#c084fc");
    }
    return seg;
}

// -------------------------------------------------------------
// ActionTimelineCanvas
// -------------------------------------------------------------
ActionTimelineCanvas::ActionTimelineCanvas(QWidget* parent)
    : QWidget(parent) {
    setMinimumHeight(64);
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Preferred);
    setMouseTracking(true);
    setFocusPolicy(Qt::StrongFocus);
}

void ActionTimelineCanvas::setDuration(double durationS) {
    m_durationS = std::max(1.0, durationS);
    update();
}

void ActionTimelineCanvas::setCurrentTime(double timeS) {
    m_currentTimeS = std::clamp(timeS, 0.0, m_durationS);
    update();
}

void ActionTimelineCanvas::setActions(const QVector<ActionSegment>& actions) {
    m_actions = actions;
    // Compute required height based on unique actors
    QStringList actors;
    for (const auto& a : m_actions) {
        if (!actors.contains(a.actorPath)) {
            actors.append(a.actorPath);
        }
    }
    const int numLanes = std::max<int>(1, static_cast<int>(actors.size()));
    setFixedHeight(kRulerHeight + numLanes * kTrackHeight + 10);
    update();
}

void ActionTimelineCanvas::setSelectedActionId(const QString& id) {
    m_selectedActionId = id;
    update();
}

void ActionTimelineCanvas::setHighlightedActorPath(const QString& actorPath) {
    m_highlightedActorPath = actorPath;
    update();
}

double ActionTimelineCanvas::timeFromX(double x) const {
    const double availWidth = std::max(10.0, static_cast<double>(width() - kHeaderWidth - 10));
    const double relX = std::clamp(x - kHeaderWidth, 0.0, availWidth);
    return (relX / availWidth) * m_durationS;
}

double ActionTimelineCanvas::xFromTime(double timeS) const {
    const double availWidth = std::max(10.0, static_cast<double>(width() - kHeaderWidth - 10));
    const double norm = std::clamp(timeS / m_durationS, 0.0, 1.0);
    return kHeaderWidth + norm * availWidth;
}

ActionTimelineCanvas::HitResult ActionTimelineCanvas::hitTest(const QPoint& pt) const {
    HitResult hr;
    if (pt.y() < kRulerHeight) {
        if (pt.x() >= kHeaderWidth) {
            hr.zone = HitZone::Ruler;
        }
        return hr;
    }

    QStringList actors;
    for (const auto& a : m_actions) {
        if (!actors.contains(a.actorPath)) actors.append(a.actorPath);
    }

    const int laneIdx = (pt.y() - kRulerHeight) / kTrackHeight;
    if (laneIdx < 0 || laneIdx >= actors.size()) return hr;
    const QString actorPath = actors.at(laneIdx);
    hr.actorPath = actorPath;

    if (pt.x() < kHeaderWidth) {
        hr.zone = HitZone::HeaderActor;
        return hr;
    }

    const int laneY = kRulerHeight + laneIdx * kTrackHeight;
    if (pt.y() < laneY + 2 || pt.y() > laneY + kTrackHeight - 2) return hr;

    // Check actions on this lane
    for (const auto& a : m_actions) {
        if (a.actorPath != actorPath) continue;

        const double x1 = xFromTime(a.startS);
        const double x2 = xFromTime(a.endS);

        if (!a.autoTiming && !a.generated && std::abs(pt.x() - x1) <= 6) {
            hr.zone = HitZone::BlockLeftEdge;
            hr.actionId = a.id;
            return hr;
        } else if (!a.autoTiming && !a.generated && std::abs(pt.x() - x2) <= 6) {
            hr.zone = HitZone::BlockRightEdge;
            hr.actionId = a.id;
            return hr;
        } else if (pt.x() >= x1 && pt.x() <= x2) {
            hr.zone = HitZone::BlockBody;
            hr.actionId = a.id;
            return hr;
        }
    }

    return hr;
}

void ActionTimelineCanvas::paintEvent(QPaintEvent*) {
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing, true);

    const int w = width();
    const int h = height();

    // 1. Background
    p.fillRect(rect(), QColor("#0d1117"));

    // Header column background
    p.fillRect(0, 0, kHeaderWidth, h, QColor("#161b22"));
    p.setPen(QColor("#30363d"));
    p.drawLine(kHeaderWidth, 0, kHeaderWidth, h);

    // 2. Ruler
    p.fillRect(kHeaderWidth, 0, w - kHeaderWidth, kRulerHeight, QColor("#12171f"));
    p.setPen(QColor("#21262d"));
    p.drawLine(kHeaderWidth, kRulerHeight, w, kRulerHeight);

    // Tick marks every 1s, major labels every 5s
    const double availWidth = std::max(10.0, static_cast<double>(w - kHeaderWidth - 10));
    const double pxPerSec = availWidth / m_durationS;
    const double secStep = pxPerSec > 40.0 ? 1.0 : (pxPerSec > 15.0 ? 2.0 : 5.0);

    QFont rulerFont = font();
    rulerFont.setPixelSize(10);
    p.setFont(rulerFont);

    for (double s = 0.0; s <= m_durationS + 0.01; s += secStep) {
        const double x = xFromTime(s);
        const bool isMajor = (std::fmod(s, 5.0) < 0.01 || std::abs(s - m_durationS) < 0.01);
        p.setPen(isMajor ? QColor("#8b949e") : QColor("#30363d"));
        p.drawLine(QPointF(x, isMajor ? 6 : 14), QPointF(x, kRulerHeight));
        if (isMajor) {
            p.drawText(QRectF(x - 20, 1, 40, 14), Qt::AlignCenter, QString("%1s").arg(s, 0, 'f', 0));
        }
    }

    // 3. Unique Actor Lanes
    QStringList actors;
    QHash<QString, QString> actorLabels;
    QHash<QString, QColor> actorColors;
    for (const auto& a : m_actions) {
        if (!actors.contains(a.actorPath)) {
            actors.append(a.actorPath);
            actorLabels[a.actorPath] = a.actorLabel.isEmpty() ? a.actorPath.section('/', -1) : a.actorLabel;
            actorColors[a.actorPath] = a.color;
        }
    }

    if (actors.isEmpty()) {
        p.setPen(QColor("#484f58"));
        QFont italicF = font();
        italicF.setItalic(true);
        italicF.setPixelSize(11);
        p.setFont(italicF);
        p.drawText(QRectF(kHeaderWidth + 16, kRulerHeight + 8, w - kHeaderWidth - 32, 24),
                   Qt::AlignLeft | Qt::AlignVCenter,
                   "Choose an asset and behavior. Movement time follows its route.");
        return;
    }

    // Draw lanes
    for (int i = 0; i < actors.size(); ++i) {
        const int laneY = kRulerHeight + i * kTrackHeight;
        const QString actorPath = actors.at(i);
        const QString label = actorLabels.value(actorPath);
        const QColor color = actorColors.value(actorPath, QColor("#58a6ff"));
        const bool isActorHighlighted = (!m_highlightedActorPath.isEmpty() && m_highlightedActorPath == actorPath);

        // Track lane background
        if (isActorHighlighted) {
            p.fillRect(kHeaderWidth + 1, laneY, w - kHeaderWidth - 1, kTrackHeight, QColor(88, 166, 255, 20));
        } else if (i % 2 == 1) {
            p.fillRect(kHeaderWidth + 1, laneY, w - kHeaderWidth - 1, kTrackHeight, QColor(255, 255, 255, 4));
        }
        p.setPen(QColor("#21262d"));
        p.drawLine(0, laneY + kTrackHeight, w, laneY + kTrackHeight);

        // Header Actor Pill
        p.save();
        p.setRenderHint(QPainter::Antialiasing, true);
        const QRectF pillRect(6, laneY + 4, kHeaderWidth - 12, kTrackHeight - 8);
        p.setBrush(QColor(color.red(), color.green(), color.blue(), isActorHighlighted ? 60 : 30));
        p.setPen(QPen(isActorHighlighted ? QColor("#58a6ff") : color, isActorHighlighted ? 2.0 : 1.2));
        p.drawRoundedRect(pillRect, 4, 4);

        p.setPen(QColor("#ffffff"));
        QFont lFont = font();
        lFont.setBold(true);
        lFont.setPixelSize(10);
        p.setFont(lFont);
        const QString icon = (actorPath.contains("car") || actorPath.contains("vehicle") || actorPath.contains("Mustang")) ? "🚗 " : "👤 ";
        p.drawText(pillRect.adjusted(6, 0, -6, 0), Qt::AlignVCenter | Qt::AlignLeft,
                   p.fontMetrics().elidedText(icon + label, Qt::ElideRight, static_cast<int>(pillRect.width() - 12)));
        p.restore();

        // Grid lines per 5 seconds across track
        p.setPen(QPen(QColor("#1f242c"), 1.0, Qt::DotLine));
        for (double s = 5.0; s < m_durationS; s += 5.0) {
            const double x = xFromTime(s);
            p.drawLine(QPointF(x, laneY), QPointF(x, laneY + kTrackHeight));
        }

        QVector<ActionSegment> ordered;
        for (const auto& seg : m_actions) if (seg.actorPath == actorPath) ordered.append(seg);
        std::sort(ordered.begin(), ordered.end(), [](const auto& a, const auto& b) { return a.startS < b.startS; });
        double gapStart = 0;
        p.setPen(QColor("#697586"));
        for (int n = 0; n <= ordered.size(); ++n) {
            const double gapEnd = n < ordered.size() ? ordered[n].startS : m_durationS;
            if (gapEnd-gapStart > .15) p.drawText(QRectF(xFromTime(gapStart), laneY, xFromTime(gapEnd)-xFromTime(gapStart), kTrackHeight), Qt::AlignCenter, "open");
            if (n < ordered.size()) gapStart = qMax(gapStart, ordered[n].endS);
        }

        // Draw Action Blocks for this actor
        for (const auto& a : m_actions) {
            if (a.actorPath != actorPath) continue;

            const double x1 = xFromTime(a.startS);
            const double x2 = xFromTime(a.endS);
            const double blockW = std::max(6.0, x2 - x1);
            const QRectF blockRect(x1, laneY + 3, blockW, 27);

            QString selectedGroup;
            for (const auto& selected : m_actions) if (selected.id == m_selectedActionId) {
                selectedGroup = selected.groupId; break;
            }
            const bool isSelected = a.id == m_selectedActionId ||
                (!selectedGroup.isEmpty() && a.groupId == selectedGroup);
            const bool isActiveNow = (m_currentTimeS >= a.startS && m_currentTimeS <= a.endS);

            p.save();
            // Block background & border
            const QColor actCol = a.generated ? QColor("#22d3ee") : (a.color.isValid() ? a.color : color);
            QColor fillCol = actCol;
            fillCol.setAlpha(isSelected ? 130 : (isActiveNow ? 100 : 55));
            p.setBrush(fillCol);
            p.setPen(QPen(isSelected ? QColor("#ffffff") : (isActiveNow ? QColor("#fbbf24") : actCol),
                          isSelected ? 2.0 : (isActiveNow ? 1.8 : 1.2)));
            p.drawRoundedRect(blockRect, 4, 4);

            // If selected, draw edge resize grip lines on left and right borders
            if (isSelected && !a.autoTiming && !a.generated && blockW >= 16.0) {
                p.setPen(Qt::NoPen);
                p.setBrush(QColor(255, 255, 255, 230));
                p.drawRoundedRect(QRectF(x1 + 2, laneY + 8, 2, 16), 1, 1);
                p.drawRoundedRect(QRectF(x2 - 4, laneY + 8, 2, 16), 1, 1);
            }

            // Block Text: e.g. "0s-5s walk" or "phone call"
            p.setPen(QColor("#ffffff"));
            QFont bFont = font();
            bFont.setPixelSize(10);
            bFont.setBold(isSelected || isActiveNow);
            p.setFont(bFont);

            const QString timeTag = QString("%1s-%2s").arg(a.startS, 0, 'f', 1).arg(a.endS, 0, 'f', 1);
            const QString blockText = a.generated
                ? QString("✦ AI %1 %2").arg(timeTag, a.action)
                : a.groupId.isEmpty()
                ? QString("%1 %2 %3%4").arg(a.lock == "locked" ? "🔒" : a.lock == "preferred" ? "◇" : "○",
                                           timeTag, a.action, a.autoTiming ? " · auto" : "")
                : QString("◆ %1 %2 · %3 people").arg(timeTag, a.action).arg(a.participants.size());

            const QRectF textRect = blockRect.adjusted(8, 0, -8, 0);
            p.drawText(textRect, Qt::AlignVCenter | Qt::AlignLeft,
                       p.fontMetrics().elidedText(blockText, Qt::ElideRight, static_cast<int>(textRect.width())));

            if (a.distanceM >= 0.0 && blockW >= 24.0) {
                const double left = x1 + 7.0;
                const double right = x2 - 7.0;
                const double guideY = laneY + 37.0;
                p.setPen(QPen(QColor("#7dd3fc"), 1.5));
                p.drawLine(QPointF(left, guideY), QPointF(right, guideY));
                for (int tick = 0; tick <= 4; ++tick) {
                    const double x = left + (right-left)*tick/4.0;
                    p.drawLine(QPointF(x, guideY-3), QPointF(x, guideY+3));
                }
                if (blockW >= 92.0) {
                    QFont distanceFont = font();
                    distanceFont.setPixelSize(9);
                    p.setFont(distanceFont);
                    p.setPen(QColor("#d6f2ff"));
                    p.drawText(QRectF(left+3, laneY+36, right-left-6, 11), Qt::AlignRight,
                               QString("%1 m").arg(a.distanceM, 0, 'f', 1));
                }
            }

            p.restore();
        }
    }

    // 4. Scrubber Playhead
    const double curX = xFromTime(m_currentTimeS);
    p.save();
    p.setRenderHint(QPainter::Antialiasing, true);

    // Vertical line
    p.setPen(QPen(QColor("#f59e0b"), 1.8));
    p.drawLine(QPointF(curX, 0), QPointF(curX, h));

    // Top Triangle Badge
    QPolygonF triangle;
    triangle << QPointF(curX - 6, 0) << QPointF(curX + 6, 0)
             << QPointF(curX + 6, 9) << QPointF(curX, 15)
             << QPointF(curX - 6, 9);
    p.setBrush(QColor("#f59e0b"));
    p.setPen(Qt::NoPen);
    p.drawPolygon(triangle);

    p.restore();
}

void ActionTimelineCanvas::mousePressEvent(QMouseEvent* event) {
    if (event->button() == Qt::LeftButton) {
        const HitResult hr = hitTest(event->pos());

        if (hr.zone == HitZone::HeaderActor) {
            emit actorSelected(hr.actorPath);
            return;
        }

        if (hr.zone == HitZone::BlockLeftEdge) {
            m_selectedActionId = hr.actionId;
            m_dragMode = DragMode::ResizeStart;
            m_dragActionId = hr.actionId;
            for (const auto& a : m_actions) {
                if (a.id == hr.actionId) {
                    m_dragInitialStartS = a.startS;
                    m_dragInitialEndS = a.endS;
                    emit actorSelected(a.actorPath);
                    break;
                }
            }
            m_dragInitialMouseTime = timeFromX(event->pos().x());
            m_hasDragged = false;
            emit actionSelected(hr.actionId);
            update();
            return;
        }

        if (hr.zone == HitZone::BlockRightEdge) {
            m_selectedActionId = hr.actionId;
            m_dragMode = DragMode::ResizeEnd;
            m_dragActionId = hr.actionId;
            for (const auto& a : m_actions) {
                if (a.id == hr.actionId) {
                    m_dragInitialStartS = a.startS;
                    m_dragInitialEndS = a.endS;
                    emit actorSelected(a.actorPath);
                    break;
                }
            }
            m_dragInitialMouseTime = timeFromX(event->pos().x());
            m_hasDragged = false;
            emit actionSelected(hr.actionId);
            update();
            return;
        }

        if (hr.zone == HitZone::BlockBody) {
            for (const auto& a : m_actions) if (a.id == hr.actionId && a.generated) {
                m_selectedActionId = hr.actionId;
                emit actionSelected(hr.actionId);
                emit timeScrubbed(a.startS);
                update();
                return;
            }
            m_selectedActionId = hr.actionId;
            m_dragMode = DragMode::MoveSegment;
            m_dragActionId = hr.actionId;
            for (const auto& a : m_actions) {
                if (a.id == hr.actionId) {
                    m_dragInitialStartS = a.startS;
                    m_dragInitialEndS = a.endS;
                    emit actorSelected(a.actorPath);
                    emit timeScrubbed(a.startS);
                    break;
                }
            }
            m_dragInitialMouseTime = timeFromX(event->pos().x());
            m_hasDragged = false;
            emit actionSelected(hr.actionId);
            update();
            return;
        }

        // Empty track area or ruler -> Scrubbing
        m_selectedActionId.clear();
        emit actionSelected(QString());
        m_dragMode = DragMode::Scrub;
        const double t = timeFromX(event->pos().x());
        emit timeScrubbed(t);
        update();
    } else if (event->button() == Qt::RightButton) {
        const HitResult hr = hitTest(event->pos());
        if (!hr.actionId.isEmpty()) {
            m_selectedActionId = hr.actionId;
            emit actionSelected(hr.actionId);
            update();

            QMenu menu(this);
            QAction* editAct = menu.addAction("✏️ Edit Action");
            QAction* dupAct = menu.addAction("📋 Duplicate Action");
            menu.addSeparator();
            QAction* delAct = menu.addAction("🗑 Delete Action Segment");
            for (const auto& a : m_actions) if (a.id == hr.actionId && a.generated) {
                editAct->setText("Pin AI action to timeline");
                dupAct->setEnabled(false);
                delAct->setEnabled(false);
                break;
            }
            QAction* chosen = menu.exec(event->globalPosition().toPoint());

            if (chosen == editAct) {
                if (editAct->text() == "Pin AI action to timeline") emit actionPinRequested(hr.actionId);
                else emit actionSelected(hr.actionId);
            } else if (chosen == dupAct) {
                emit actionDuplicateRequested(hr.actionId);
            } else if (chosen == delAct) {
                emit actionDeleteRequested(hr.actionId);
            }
        }
    }
}

void ActionTimelineCanvas::mouseMoveEvent(QMouseEvent* event) {
    auto syncGroupWindow = [this](const ActionSegment& source) {
        if (source.groupId.isEmpty()) return;
        for (auto& other : m_actions) if (other.groupId == source.groupId) {
            other.startS = source.startS;
            other.endS = source.endS;
        }
    };
    if (event->buttons() & Qt::LeftButton) {
        if (m_dragMode == DragMode::ResizeStart) {
            for (auto& a : m_actions) {
                if (a.id == m_dragActionId) {
                    const double curT = timeFromX(event->pos().x());
                    double newStart = std::clamp(curT, 0.0, m_dragInitialEndS - 0.2);
                    newStart = std::round(newStart * 10.0) / 10.0;
                    if (std::abs(a.startS - newStart) > 0.01) {
                        if (!m_hasDragged) emit actionEditStarted();
                        a.startS = newStart;
                        syncGroupWindow(a);
                        m_hasDragged = true;
                        emit actionLiveUpdated(a);
                        update();
                    }
                    break;
                }
            }
            return;
        }

        if (m_dragMode == DragMode::ResizeEnd) {
            for (auto& a : m_actions) {
                if (a.id == m_dragActionId) {
                    const double curT = timeFromX(event->pos().x());
                    double newEnd = std::max(m_dragInitialStartS + 0.2, curT);
                    newEnd = std::round(newEnd * 10.0) / 10.0;
                    if (std::abs(a.endS - newEnd) > 0.01) {
                        if (!m_hasDragged) emit actionEditStarted();
                        a.endS = newEnd;
                        syncGroupWindow(a);
                        m_hasDragged = true;
                        emit actionLiveUpdated(a);
                        update();
                    }
                    break;
                }
            }
            return;
        }

        if (m_dragMode == DragMode::MoveSegment) {
            for (auto& a : m_actions) {
                if (a.id == m_dragActionId) {
                    const double curT = timeFromX(event->pos().x());
                    const double dt = curT - m_dragInitialMouseTime;
                    const double dur = m_dragInitialEndS - m_dragInitialStartS;
                    double newStart = std::clamp(m_dragInitialStartS + dt, 0.0, std::max(m_durationS, m_dragInitialEndS) - dur);
                    newStart = std::round(newStart * 10.0) / 10.0;
                    if (std::abs(a.startS - newStart) > 0.01) {
                        if (!m_hasDragged) emit actionEditStarted();
                        a.startS = newStart;
                        a.endS = newStart + dur;
                        syncGroupWindow(a);
                        m_hasDragged = true;
                        emit actionLiveUpdated(a);
                        update();
                    }
                    break;
                }
            }
            return;
        }

        if (m_dragMode == DragMode::Scrub) {
            const double t = timeFromX(event->pos().x());
            emit timeScrubbed(t);
            update();
            return;
        }
    } else {
        // Hover state & cursor updates
        const HitResult hr = hitTest(event->pos());
        if (hr.zone == HitZone::BlockLeftEdge || hr.zone == HitZone::BlockRightEdge) {
            setCursor(Qt::SizeHorCursor);
        } else if (hr.zone == HitZone::BlockBody) {
            setCursor(Qt::SizeAllCursor);
        } else if (hr.zone == HitZone::HeaderActor) {
            setCursor(Qt::PointingHandCursor);
        } else if (hr.zone == HitZone::Ruler) {
            setCursor(Qt::SplitHCursor);
        } else {
            setCursor(Qt::ArrowCursor);
        }

        if (!hr.actionId.isEmpty()) {
            for (const auto& a : m_actions) {
                if (a.id == hr.actionId) {
                    QToolTip::showText(event->globalPosition().toPoint(),
                                       QString("<b>%1</b>: %2%6<br/>Timing: <b>%3s – %4s</b> (duration: %5s)<br/><i>%7</i>")
                                           .arg(a.actorLabel, a.action)
                                           .arg(a.startS, 0, 'f', 1)
                                           .arg(a.endS, 0, 'f', 1)
                                           .arg(a.endS - a.startS, 0, 'f', 1)
                                           .arg(a.generated ? " · generated from prompt" : "")
                                           .arg(a.generated ? "Select and pin to make this an authored timeline action" :
                                                "Click to select & edit • Drag edges to resize • Drag body to move • Right-click for menu"),
                                       this);
                    break;
                }
            }
        }
    }
}

void ActionTimelineCanvas::mouseReleaseEvent(QMouseEvent* event) {
    if (event->button() == Qt::LeftButton) {
        if (m_hasDragged) {
            emit actionEditFinished();
        }
        m_dragMode = DragMode::None;
        m_hasDragged = false;
    }
}

void ActionTimelineCanvas::mouseDoubleClickEvent(QMouseEvent* event) {
    if (event->button() == Qt::LeftButton) {
        const HitResult hr = hitTest(event->pos());
        if (!hr.actionId.isEmpty()) {
            m_selectedActionId = hr.actionId;
            emit actionSelected(hr.actionId);
            update();
        }
    }
}

void ActionTimelineCanvas::keyPressEvent(QKeyEvent* event) {
    if (event->matches(QKeySequence::Undo)) {
        emit undoRequested();
        event->accept();
        return;
    }
    if (event->matches(QKeySequence::Redo)) {
        emit redoRequested();
        event->accept();
        return;
    }
    if (event->key() == Qt::Key_Delete || event->key() == Qt::Key_Backspace) {
        if (!m_selectedActionId.isEmpty()) {
            emit actionDeleteRequested(m_selectedActionId);
            event->accept();
            return;
        }
    }
    if (event->key() == Qt::Key_Escape) {
        m_selectedActionId.clear();
        emit actionSelected(QString());
        update();
        event->accept();
        return;
    }
    QWidget::keyPressEvent(event);
}

// -------------------------------------------------------------
// ActionTimelineWidget
// -------------------------------------------------------------
ActionTimelineWidget::ActionTimelineWidget(QWidget* parent)
    : QWidget(parent) {
    setObjectName("actionTimelinePanel");
    setStyleSheet(
        "#actionTimelinePanel { background: #10151a; border: 1px solid #30363d; border-radius: 8px; }"
        "QLabel { color: #e6edf3; font-size: 11px; }"
        "QComboBox, QDoubleSpinBox, QLineEdit { background: #161b22; color: #e6edf3; border: 1px solid #30363d; border-radius: 4px; padding: 2px 6px; font-size: 11px; min-height: 22px; }"
        "QDoubleSpinBox QLineEdit { background: transparent; color: #e6edf3; border: none; selection-background-color: #388bfd; }"
        "QComboBox QAbstractItemView { background: #161b22; color: #e6edf3; selection-background-color: #264f7d; selection-color: #ffffff; }"
        "QComboBox:hover, QDoubleSpinBox:hover { border-color: #58a6ff; }"
        "QPushButton { background: #21262d; color: #e6edf3; border: 1px solid #30363d; border-radius: 4px; padding: 4px 10px; font-size: 11px; font-weight: 600; min-height: 22px; }"
        "QPushButton:hover { background: #30363d; border-color: #8b949e; }"
        "QPushButton:disabled { color: #8b949e; background: #161b22; border-color: #30363d; }"
        "QPushButton#primaryActionBtn { background: #1f6feb; border-color: #388bfd; color: #ffffff; }"
        "QPushButton#primaryActionBtn:hover { background: #388bfd; }"
        "QPushButton#updateActionBtn { background: #238636; border-color: #2ea043; color: #ffffff; }"
        "QPushButton#updateActionBtn:hover { background: #2ea043; }"
        "QPushButton#dangerBtn { background: #21262d; border-color: #da3633; color: #f85149; }"
        "QPushButton#dangerBtn:hover { background: #b62324; color: #ffffff; }");

    QVBoxLayout* rootLayout = new QVBoxLayout(this);
    rootLayout->setContentsMargins(10, 6, 10, 8);
    rootLayout->setSpacing(6);

    // --- Header Bar ---
    QHBoxLayout* headerLayout = new QHBoxLayout();
    headerLayout->setSpacing(8);

    m_titleLabel = new QLabel("🎬 SHOT ACTION TIMELINE", this);
    m_titleLabel->setStyleSheet("font-weight: 700; font-size: 12px; color: #58a6ff; letter-spacing: 0.5px;");
    headerLayout->addWidget(m_titleLabel);

    headerLayout->addSpacing(8);

    // Duration input
    QLabel* durLbl = new QLabel("Duration:", this);
    durLbl->setStyleSheet("color: #8b949e;");
    headerLayout->addWidget(durLbl);

    m_durationSpin = new QDoubleSpinBox(this);
    m_durationSpin->setRange(1.0, 3600.0);
    m_durationSpin->setValue(15.0);
    m_durationSpin->setSingleStep(1.0);
    m_durationSpin->setDecimals(1);
    m_durationSpin->setSuffix(" s");
    m_durationSpin->setFixedWidth(70);
    m_durationSpin->setToolTip("Total shot duration in seconds");
    connect(m_durationSpin, QOverload<double>::of(&QDoubleSpinBox::valueChanged), this, [this](double val) {
        if (std::abs(m_durationS - val) > 0.01) {
            pushUndoState("Change Duration");
            m_durationS = val;
            if (m_canvas) m_canvas->setDuration(val);
            updateTimeLabel();
            emit durationChanged(val);
            emit actionsChanged();
        }
    });
    headerLayout->addWidget(m_durationSpin);

    headerLayout->addSpacing(8);

    // Scrubber time readout
    m_timeLabel = new QLabel("⏱ 00.0s / 15.0s", this);
    m_timeLabel->setStyleSheet("font-family: monospace; font-weight: 700; color: #f59e0b; padding: 2px 6px; background: #1f1a14; border: 1px solid #78350f; border-radius: 4px;");
    headerLayout->addWidget(m_timeLabel);

    headerLayout->addStretch(1);

    // Timeline Undo / Redo buttons
    m_undoButton = new QPushButton("↶ Undo", this);
    m_undoButton->setCursor(Qt::PointingHandCursor);
    m_undoButton->setToolTip("Undo timeline edit (Cmd+Z / Ctrl+Z)");
    m_undoButton->setEnabled(false);
    connect(m_undoButton, &QPushButton::clicked, this, &ActionTimelineWidget::undo);
    headerLayout->addWidget(m_undoButton);
    m_undoButton->hide();

    m_redoButton = new QPushButton("↷ Redo", this);
    m_redoButton->setCursor(Qt::PointingHandCursor);
    m_redoButton->setToolTip("Redo timeline edit (Cmd+Shift+Z / Ctrl+Y)");
    m_redoButton->setEnabled(false);
    connect(m_redoButton, &QPushButton::clicked, this, &ActionTimelineWidget::redo);
    headerLayout->addWidget(m_redoButton);
    m_redoButton->hide();

    headerLayout->addSpacing(4);

    m_quickAddToggle = new QPushButton("+ Add Action Segment", this);
    m_quickAddToggle->setCursor(Qt::PointingHandCursor);
    m_quickAddToggle->setToolTip("Toggle action input controls");
    connect(m_quickAddToggle, &QPushButton::clicked, this, [this]() {
        if (m_inputBar) {
            m_inputBar->setVisible(!m_inputBar->isVisible());
        }
    });
    headerLayout->addWidget(m_quickAddToggle);
    m_quickAddToggle->hide();

    auto* sharedButton = new QPushButton("+ Shared Action", this);
    sharedButton->setToolTip("Add or edit one action for selected participants");
    connect(sharedButton, &QPushButton::clicked, this, &ActionTimelineWidget::editSharedAction);
    headerLayout->addWidget(sharedButton);

    m_syncPromptButton = new QPushButton("Preview brief", this);
    m_syncPromptButton->setCursor(Qt::PointingHandCursor);
    m_syncPromptButton->setToolTip("Preview the qualitative director brief");
    connect(m_syncPromptButton, &QPushButton::clicked, this, [this]() {
        emit syncPromptRequested(generateSummaryPrompt());
    });
    headerLayout->addWidget(m_syncPromptButton);
    m_syncPromptButton->hide();
    auto* templates = new QComboBox(this); templates->addItems({"Template…", "Approach · talk · leave", "Chase · hide"});
    headerLayout->addWidget(templates);
    templates->hide();
    connect(templates, &QComboBox::activated, this, [this, templates](int index) {
        if (!index || m_actorCombo->currentData().toString().isEmpty()) return;
        const QStringList verbs = index == 1 ? QStringList{"walk_to","converse","walk_to"} : QStringList{"run_to","take_cover"};
        double start = m_currentTimeS;
        for (int i=0; i<verbs.size(); ++i) { ActionSegment seg; seg.id = "template_"+QString::number(QDateTime::currentMSecsSinceEpoch())+"_"+QString::number(i);
            seg.actorPath=m_actorCombo->currentData().toString(); seg.actorLabel=m_actorCombo->currentText(); seg.startS=start;seg.endS=start+4;seg.action=verbs[i];seg.target=m_targetCombo->currentData().toString();addAction(seg);start+=4; }
        templates->setCurrentIndex(0);
    });

    m_clearButton = new QPushButton("✕ Clear", this);
    m_clearButton->setCursor(Qt::PointingHandCursor);
    m_clearButton->setToolTip("Clear all action segments");
    connect(m_clearButton, &QPushButton::clicked, this, &ActionTimelineWidget::clearActions);
    headerLayout->addWidget(m_clearButton);
    m_clearButton->hide();

    m_toggleCollapseButton = new QPushButton("▲", this);
    m_toggleCollapseButton->setFixedWidth(28);
    m_toggleCollapseButton->setCursor(Qt::PointingHandCursor);
    m_toggleCollapseButton->setToolTip("Collapse / Expand Timeline Panel");
    connect(m_toggleCollapseButton, &QPushButton::clicked, this, &ActionTimelineWidget::toggleCollapse);
    headerLayout->addWidget(m_toggleCollapseButton);

    rootLayout->addLayout(headerLayout);

    // --- Quick Add / Edit Input Bar ---
    m_inputBar = new QWidget(this);
    QHBoxLayout* inputLayout = new QHBoxLayout(m_inputBar);
    inputLayout->setContentsMargins(0, 2, 0, 2);
    inputLayout->setSpacing(6);

    m_modeLabel = new QLabel("➕ Add Mode", m_inputBar);
    m_modeLabel->setStyleSheet("padding: 2px 6px; border-radius: 4px; font-weight: 600; font-size: 11px; color: #8b949e; background: #161b22; border: 1px solid #30363d;");
    inputLayout->addWidget(m_modeLabel);
    m_modeLabel->hide();

    QLabel* actorLbl = new QLabel("Asset:", m_inputBar);
    actorLbl->setStyleSheet("color: #8b949e;");
    inputLayout->addWidget(actorLbl);

    m_actorCombo = new QComboBox(m_inputBar);
    m_actorCombo->setMinimumWidth(150);
    connect(m_actorCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &ActionTimelineWidget::handleActorComboChanged);
    inputLayout->addWidget(m_actorCombo);

    QLabel* fromLbl = new QLabel("From:", m_inputBar);
    fromLbl->setStyleSheet("color: #8b949e;");
    inputLayout->addWidget(fromLbl);

    m_startSpin = new QDoubleSpinBox(m_inputBar);
    m_startSpin->setRange(0.0, 3600.0);
    m_startSpin->setValue(0.0);
    m_startSpin->setSingleStep(0.5);
    m_startSpin->setDecimals(1);
    m_startSpin->setSuffix("s");
    m_startSpin->setFixedWidth(64);
    inputLayout->addWidget(m_startSpin);

    QLabel* toLbl = new QLabel("To:", m_inputBar);
    toLbl->setStyleSheet("color: #8b949e;");
    inputLayout->addWidget(toLbl);

    m_endSpin = new QDoubleSpinBox(m_inputBar);
    m_endSpin->setRange(0.1, 3600.0);
    m_endSpin->setValue(5.0);
    m_endSpin->setSingleStep(0.5);
    m_endSpin->setDecimals(1);
    m_endSpin->setSuffix("s");
    m_endSpin->setFixedWidth(64);
    inputLayout->addWidget(m_endSpin);

    QLabel* actLbl = new QLabel("Behavior:", m_inputBar);
    actLbl->setStyleSheet("color: #8b949e;");
    inputLayout->addWidget(actLbl);

    m_actionCombo = new QComboBox(m_inputBar);
    m_actionCombo->setEditable(false);
    m_actionCombo->setMinimumWidth(145);
    m_actionCombo->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    m_actionCombo->addItems({"walk_to", "run_to", "jog_to", "use_phone", "shake_hands",
        "converse", "greet", "stand_up", "turn_to_face", "attack", "drive_to", "wait", "look_at", "sneak_to", "flee_from", "take_cover"});
    m_lockCombo = new QComboBox(m_inputBar);
    m_lockCombo->addItems({"locked", "preferred", "open"});
    m_lockCombo->setToolTip("Windows stay exact for every lock level");
    m_targetCombo = new QComboBox(m_inputBar);
    m_targetCombo->addItem("No target", QString());

    if (m_actionCombo->lineEdit()) {
        connect(m_actionCombo->lineEdit(), &QLineEdit::returnPressed, this, &ActionTimelineWidget::handleAddOrUpdateButtonClicked);
    }
    inputLayout->addWidget(m_actionCombo, 1);
    inputLayout->addWidget(m_lockCombo);
    m_speedCombo = new QComboBox(m_inputBar);m_speedCombo->addItems({"constant","ease_in_out"});inputLayout->addWidget(m_speedCombo);
    m_noteEdit = new QLineEdit(m_inputBar);
    m_noteEdit->setPlaceholderText("Style note"); inputLayout->addWidget(m_noteEdit);
    m_toleranceSpin = new QDoubleSpinBox(m_inputBar);
    m_toleranceSpin->setRange(0, 30); m_toleranceSpin->setSuffix(" s flex");
    m_toleranceSpin->setToolTip("Explicit timing flexibility; zero keeps exact windows");
    inputLayout->addWidget(m_toleranceSpin);
    auto* drawSegment = new QPushButton("Draw segment path", m_inputBar);
    inputLayout->addWidget(drawSegment);
    connect(drawSegment, &QPushButton::clicked, this, [this]() {
        auto seg = selectedSegment(); if (seg.id.isEmpty()) return;
        if (seg.pathId.isEmpty()) { seg.pathId = "path_" + seg.id; updateAction(seg); }
        emit drawSegmentPathRequested(seg.actorPath, seg.pathId);
    });
    inputLayout->addWidget(m_targetCombo);
    auto* pickTarget = new QPushButton("Pick target", m_inputBar);inputLayout->addWidget(pickTarget);
    connect(pickTarget, &QPushButton::clicked, this, &ActionTimelineWidget::targetPickRequested);

    m_addButton = new QPushButton("+ Add Behavior", m_inputBar);
    m_addButton->setObjectName("primaryActionBtn");
    m_addButton->setCursor(Qt::PointingHandCursor);
    connect(m_addButton, &QPushButton::clicked, this, &ActionTimelineWidget::handleAddOrUpdateButtonClicked);
    inputLayout->addWidget(m_addButton);

    m_cancelEditButton = new QPushButton("✕ Deselect", m_inputBar);
    m_cancelEditButton->setCursor(Qt::PointingHandCursor);
    m_cancelEditButton->setToolTip("Deselect to add a new action segment");
    m_cancelEditButton->setVisible(false);
    connect(m_cancelEditButton, &QPushButton::clicked, this, &ActionTimelineWidget::handleCancelEditClicked);
    inputLayout->addWidget(m_cancelEditButton);

    m_deleteActionButton = new QPushButton("🗑 Delete", m_inputBar);
    m_deleteActionButton->setObjectName("dangerBtn");
    m_deleteActionButton->setCursor(Qt::PointingHandCursor);
    m_deleteActionButton->setToolTip("Delete selected action segment");
    m_deleteActionButton->setVisible(false);
    connect(m_deleteActionButton, &QPushButton::clicked, this, &ActionTimelineWidget::handleDeleteSelectedClicked);
    inputLayout->addWidget(m_deleteActionButton);

    rootLayout->addWidget(m_inputBar);

    // Keep the everyday workflow focused on asset, time, and behavior.
    // Optional directing parameters remain available when needed.
    auto* optionsButton = new QPushButton("More options ▾", m_inputBar);
    optionsButton->setToolTip("Target, path, timing flexibility, and style");
    inputLayout->insertWidget(inputLayout->indexOf(m_addButton), optionsButton);
    m_advancedBar = new QWidget(this);
    auto* advancedLayout = new QHBoxLayout(m_advancedBar);
    advancedLayout->setContentsMargins(0, 0, 0, 0);
    advancedLayout->setSpacing(6);
    auto moveToAdvanced = [inputLayout, advancedLayout, this](QWidget* widget) {
        inputLayout->removeWidget(widget);
        widget->setParent(m_advancedBar);
        advancedLayout->addWidget(widget);
    };
    advancedLayout->addWidget(new QLabel("Target:", m_advancedBar));
    moveToAdvanced(m_targetCombo);
    moveToAdvanced(pickTarget);
    advancedLayout->addWidget(new QLabel("Timing:", m_advancedBar));
    moveToAdvanced(m_lockCombo);
    moveToAdvanced(m_toleranceSpin);
    advancedLayout->addWidget(new QLabel("Speed:", m_advancedBar));
    moveToAdvanced(m_speedCombo);
    moveToAdvanced(m_noteEdit);
    moveToAdvanced(drawSegment);
    advancedLayout->addStretch(1);
    m_advancedBar->hide();
    connect(optionsButton, &QPushButton::clicked, this, [this, optionsButton]() {
        m_advancedExpanded = !m_advancedExpanded;
        m_advancedBar->setVisible(m_advancedExpanded);
        optionsButton->setText(m_advancedExpanded ? "Fewer options ▴" : "More options ▾");
    });
    rootLayout->addWidget(m_advancedBar);

    // --- Interactive Canvas ---
    m_canvas = new ActionTimelineCanvas(this);
    connect(m_canvas, &ActionTimelineCanvas::timeScrubbed, this, [this](double t) {
        m_currentTimeS = t;
        updateTimeLabel();
        emit timeScrubbed(t);
    });
    connect(m_canvas, &ActionTimelineCanvas::actionSelected, this, &ActionTimelineWidget::handleCanvasActionSelected);
    connect(m_canvas, &ActionTimelineCanvas::actorSelected, this, &ActionTimelineWidget::handleCanvasActorSelected);
    connect(m_canvas, &ActionTimelineCanvas::actionDeleteRequested, this, &ActionTimelineWidget::removeAction);
    connect(m_canvas, &ActionTimelineCanvas::actionDuplicateRequested, this, &ActionTimelineWidget::duplicateAction);
    connect(m_canvas, &ActionTimelineCanvas::actionPinRequested, this, &ActionTimelineWidget::pinGeneratedAction);
    connect(m_canvas, &ActionTimelineCanvas::actionLiveUpdated, this, &ActionTimelineWidget::handleCanvasActionLiveUpdated);
    connect(m_canvas, &ActionTimelineCanvas::actionEditStarted, this, [this]() { pushUndoState("Adjust Action Timing"); });
    connect(m_canvas, &ActionTimelineCanvas::actionEditFinished, this, &ActionTimelineWidget::handleCanvasActionEditFinished);
    connect(m_canvas, &ActionTimelineCanvas::undoRequested, this, &ActionTimelineWidget::undo);
    connect(m_canvas, &ActionTimelineCanvas::redoRequested, this, &ActionTimelineWidget::redo);

    rootLayout->addWidget(m_canvas);

    updateTimeLabel();
    updateUndoRedoButtons();
}

void ActionTimelineWidget::setShotDuration(double seconds) {
    m_durationS = std::max(1.0, seconds);
    if (m_durationSpin) {
        QSignalBlocker b(m_durationSpin);
        m_durationSpin->setValue(m_durationS);
    }
    if (m_canvas) m_canvas->setDuration(m_durationS);
    if (m_selectedActionId.isEmpty() && m_startSpin && m_endSpin) {
        const double start = std::min(m_startSpin->value(), m_durationS - 0.1);
        m_startSpin->setValue(start);
        m_endSpin->setValue(std::max(start + 0.1, std::min(m_endSpin->value(), m_durationS)));
    }
    updateTimeLabel();
}

double ActionTimelineWidget::shotDuration() const {
    return m_durationS;
}

void ActionTimelineWidget::setCurrentTime(double seconds) {
    m_currentTimeS = std::clamp(seconds, 0.0, m_durationS);
    if (m_canvas) m_canvas->setCurrentTime(m_currentTimeS);
    updateTimeLabel();
}

void ActionTimelineWidget::setActions(const QVector<ActionSegment>& actions) {
    m_actions = actions;
    if (m_canvas) {
        QVector<ActionSegment> assetActions;
        for (const auto& action : m_actions) {
            if (action.id != "render_only_camera") assetActions.append(action);
        }
        m_canvas->setActions(assetActions);
    }

    // If an action exceeds current duration, auto-expand duration
    double maxEnd = m_durationS;
    for (const auto& a : m_actions) {
        if (a.endS > maxEnd) maxEnd = a.endS;
    }
    if (maxEnd > m_durationS) {
        setShotDuration(std::ceil(maxEnd));
    }
}

void ActionTimelineWidget::setMovementDistances(const QHash<QString, double>& distances, bool recordUndo) {
    QVector<int> order;
    for (int i = 0; i < m_actions.size(); ++i) order.append(i);
    std::sort(order.begin(), order.end(), [this](int a, int b) {
        return m_actions[a].startS == m_actions[b].startS
            ? m_actions[a].id < m_actions[b].id : m_actions[a].startS < m_actions[b].startS;
    });
    bool changed = false;
    for (int index : order) {
        auto& action = m_actions[index];
        if (action.generated) continue;
        const double distance = distances.value(action.id, -1.0);
        const double speed = nominalMovementSpeed(action.action);
        const bool automatic = speed > 0.0 && distance > 0.05;
        const double newDistance = distance > 0.05 ? distance : -1.0;
        const double duration = automatic
            ? std::max(0.2, std::round(distance/speed*10.0)/10.0)
            : action.endS-action.startS;
        const double oldEnd = action.endS;
        const double delta = automatic ? duration-(oldEnd-action.startS) : 0.0;
        if (std::abs(action.distanceM-newDistance) < 0.01 &&
            action.autoTiming == automatic && std::abs(delta) < 0.01) continue;
        if (!changed && recordUndo) pushUndoState("Fit movement to route");
        changed = true;
        action.distanceM = newDistance;
        action.autoTiming = automatic;
        if (std::abs(delta) < 0.01) continue;
        action.endS = action.startS+duration;
        for (int otherIndex = 0; otherIndex < m_actions.size(); ++otherIndex) {
            if (otherIndex == index || m_actions[otherIndex].generated ||
                m_actions[otherIndex].actorPath != action.actorPath) continue;
            auto& later = m_actions[otherIndex];
            if (later.startS >= oldEnd-0.001) {
                later.startS += delta;
                later.endS += delta;
            }
        }
    }
    if (!changed) return;
    setActions(m_actions);
    if (!m_selectedActionId.isEmpty()) selectAction(m_selectedActionId);
    if (recordUndo) emit actionsChanged();
}

void ActionTimelineWidget::addAction(const ActionSegment& action) {
    pushUndoState("Add Action: " + action.action);
    m_actions.append(action);
    setActions(m_actions);
    selectAction(action.id);
    emit actionsChanged();
}

void ActionTimelineWidget::pinGeneratedAction(const QString& id) {
    ActionSegment selected;
    for (const auto& action : m_actions) if (action.id == id) { selected = action; break; }
    if (!selected.generated) return;
    pushUndoState("Pin AI action to timeline");
    const QString idBase = QString("act_%1").arg(QDateTime::currentMSecsSinceEpoch());
    QString pinnedId;
    int member = 0;
    for (auto& action : m_actions) if (action.generated &&
        (action.id == id || (!selected.groupId.isEmpty() && action.groupId == selected.groupId))) {
        action.generated = false;
        action.id = QString("%1_%2").arg(idBase).arg(++member);
        action.color = QColor("#58a6ff");
        if (pinnedId.isEmpty()) pinnedId = action.id;
    }
    setActions(m_actions);
    selectAction(pinnedId);
    emit actionsChanged();
}

void ActionTimelineWidget::editSharedAction() {
    const ActionSegment current = selectedSegment();
    const QString oldGroup = current.generated ? QString() : current.groupId;
    QDialog dialog(this);
    dialog.setWindowTitle(oldGroup.isEmpty() ? "Add shared action" : "Edit shared action");
    auto* layout = new QVBoxLayout(&dialog);
    auto* form = new QFormLayout;
    auto* actionCombo = new QComboBox(&dialog);
    actionCombo->addItem("Group meeting", "huddle");
    actionCombo->addItem("Discussion / talking", "converse");
    actionCombo->addItem("Argument", "argue");
    actionCombo->addItem("Playing", "perform_activity");
    actionCombo->addItem("Fighting", "attack");
    const int actionIndex = actionCombo->findData(current.action);
    if (!oldGroup.isEmpty() && actionIndex >= 0) actionCombo->setCurrentIndex(actionIndex);
    form->addRow("Action:", actionCombo);
    auto* activityCombo = new QComboBox(&dialog);
    activityCombo->addItem("Cards", "cards");
    activityCombo->addItem("Gaming", "gaming");
    activityCombo->addItem("Basketball", "basketball");
    activityCombo->addItem("Soccer", "soccer");
    activityCombo->addItem("Jump rope", "jump_rope");
    const int activityIndex = activityCombo->findData(current.activity);
    if (activityIndex >= 0) activityCombo->setCurrentIndex(activityIndex);
    form->addRow("Play activity:", activityCombo);
    auto showActivity = [actionCombo, activityCombo](int) {
        activityCombo->setEnabled(actionCombo->currentData().toString() == "perform_activity");
    };
    connect(actionCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), &dialog, showActivity);
    showActivity(actionCombo->currentIndex());
    auto* start = new QDoubleSpinBox(&dialog);
    auto* end = new QDoubleSpinBox(&dialog);
    for (auto* spin : {start, end}) {
        spin->setRange(0.0, 3600.0);
        spin->setDecimals(1);
        spin->setSingleStep(0.5);
        spin->setSuffix(" s");
    }
    start->setValue(oldGroup.isEmpty() ? m_currentTimeS : current.startS);
    end->setValue(oldGroup.isEmpty() ? std::min(3600.0, m_currentTimeS + 5.0) : current.endS);
    form->addRow("From:", start);
    form->addRow("To:", end);
    layout->addLayout(form);
    layout->addWidget(new QLabel("Participants (choose at least two):", &dialog));
    auto* scroll = new QScrollArea(&dialog);
    scroll->setWidgetResizable(true);
    auto* list = new QWidget(scroll);
    auto* listLayout = new QVBoxLayout(list);
    QVector<QPair<QString, QCheckBox*>> choices;
    for (int i = 1; m_actorCombo && i < m_actorCombo->count(); ++i) {
        const QString path = m_actorCombo->itemData(i).toString();
        if (path.isEmpty() || !m_actorCombo->itemText(i).startsWith("👤")) continue;
        auto* box = new QCheckBox(m_actorCombo->itemText(i), list);
        box->setChecked(oldGroup.isEmpty() ? path == m_actorCombo->currentData().toString()
                                           : current.participants.contains(path));
        listLayout->addWidget(box);
        choices.append({path, box});
    }
    scroll->setWidget(list);
    scroll->setMinimumHeight(120);
    layout->addWidget(scroll);
    auto* feedback = new QLabel(&dialog);
    feedback->setStyleSheet("color: #f87171;");
    layout->addWidget(feedback);
    auto* buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    connect(buttons, &QDialogButtonBox::accepted, &dialog, [&]() {
        int selected = 0;
        for (const auto& choice : choices) if (choice.second->isChecked()) ++selected;
        if (selected < 2) { feedback->setText("Select at least two participants."); return; }
        if (end->value() <= start->value()) { feedback->setText("End must be after start."); return; }
        for (const auto& choice : choices) if (choice.second->isChecked()) {
            for (const auto& existing : m_actions) {
                if (existing.groupId == oldGroup && !oldGroup.isEmpty()) continue;
                if (existing.actorPath == choice.first &&
                    start->value() < existing.endS && end->value() > existing.startS) {
                    feedback->setText("The shared action overlaps an action on " + choice.second->text() + ".");
                    return;
                }
            }
        }
        dialog.accept();
    });
    layout->addWidget(buttons);
    if (dialog.exec() != QDialog::Accepted) return;

    QStringList participants;
    for (const auto& choice : choices) if (choice.second->isChecked()) participants.append(choice.first);
    const QString groupId = oldGroup.isEmpty()
        ? QString("group_%1").arg(QDateTime::currentMSecsSinceEpoch()) : oldGroup;
    const QString action = actionCombo->currentData().toString();
    pushUndoState(oldGroup.isEmpty() ? "Add shared action" : "Edit shared action");
    if (!oldGroup.isEmpty()) {
        m_actions.erase(std::remove_if(m_actions.begin(), m_actions.end(), [&](const ActionSegment& a) {
            return a.groupId == oldGroup;
        }), m_actions.end());
    }
    for (int i = 0; i < participants.size(); ++i) {
        const QString& path = participants[i];
        const int comboIndex = m_actorCombo->findData(path);
        ActionSegment seg;
        seg.id = QString("%1_%2").arg(groupId).arg(i + 1);
        seg.groupId = groupId;
        seg.participants = participants;
        seg.actorPath = path;
        seg.actorLabel = m_actorCombo->itemText(comboIndex);
        seg.actorLabel.remove("👤 "); seg.actorLabel.remove("🚗 "); seg.actorLabel.remove("📦 ");
        seg.startS = start->value(); seg.endS = end->value();
        seg.action = action;
        if (action == "perform_activity") seg.activity = activityCombo->currentData().toString();
        seg.color = QColor("#c084fc");
        m_actions.append(seg);
    }
    setActions(m_actions);
    selectAction(QString("%1_1").arg(groupId));
    emit actionsChanged();
}

void ActionTimelineWidget::updateAction(const ActionSegment& action) {
    if (action.generated) return;
    for (int i = 0; i < m_actions.size(); ++i) {
        if (m_actions[i].id == action.id) {
            pushUndoState("Update Action: " + action.action);
            m_actions[i] = action;
            if (!action.groupId.isEmpty()) for (auto& other : m_actions) if (other.groupId == action.groupId) {
                other.startS = action.startS;
                other.endS = action.endS;
                other.action = action.action;
                other.activity = action.activity;
            }
            setActions(m_actions);
            selectAction(action.id);
            emit actionsChanged();
            break;
        }
    }
}

void ActionTimelineWidget::removeAction(const QString& id) {
    for (int i = 0; i < m_actions.size(); ++i) {
        if (m_actions[i].id == id) {
            if (m_actions[i].generated) return;
            pushUndoState("Delete Action: " + m_actions[i].action);
            const QString group = m_actions[i].groupId;
            const bool selectionRemoved = m_selectedActionId == id ||
                (!group.isEmpty() && selectedSegment().groupId == group);
            if (group.isEmpty()) m_actions.removeAt(i);
            else m_actions.erase(std::remove_if(m_actions.begin(), m_actions.end(), [&](const ActionSegment& a) {
                return a.groupId == group;
            }), m_actions.end());
            if (selectionRemoved) {
                resetInputBarToAddMode();
            }
            setActions(m_actions);
            emit actionsChanged();
            break;
        }
    }
}

void ActionTimelineWidget::clearActions() {
    if (std::none_of(m_actions.begin(), m_actions.end(), [](const ActionSegment& a) { return !a.generated; })) return;
    pushUndoState("Clear All Actions");
    m_actions.erase(std::remove_if(m_actions.begin(), m_actions.end(), [](const ActionSegment& a) {
        return !a.generated;
    }), m_actions.end());
    resetInputBarToAddMode();
    setActions(m_actions);
    emit actionsChanged();
}

void ActionTimelineWidget::duplicateAction(const QString& id) {
    for (const auto& a : m_actions) {
        if (a.id == id) {
            if (a.generated) return;
            pushUndoState("Duplicate Action: " + a.action);
            if (!a.groupId.isEmpty()) {
                const QString groupId = QString("group_%1").arg(QDateTime::currentMSecsSinceEpoch());
                const QString sourceGroup = a.groupId;
                const double sourceEnd = a.endS;
                const auto originals = m_actions;
                QString firstId;
                int member = 0;
                for (auto dup : originals) if (dup.groupId == sourceGroup) {
                    dup.groupId = groupId;
                    dup.id = QString("%1_%2").arg(groupId).arg(++member);
                    const double duration = dup.endS - dup.startS;
                    dup.startS = sourceEnd; dup.endS = sourceEnd + duration;
                    if (firstId.isEmpty()) firstId = dup.id;
                    m_actions.append(dup);
                }
                setActions(m_actions); selectAction(firstId); emit actionsChanged();
                break;
            }
            ActionSegment dup = a;
            dup.id = QString("act_%1").arg(QDateTime::currentMSecsSinceEpoch());
            const double dur = a.endS - a.startS;
            dup.startS = a.endS;
            dup.endS = a.endS + dur;
            m_actions.append(dup);
            if (dup.endS > m_durationS) {
                setShotDuration(std::ceil(dup.endS));
            }
            setActions(m_actions);
            selectAction(dup.id);
            emit actionsChanged();
            break;
        }
    }
}

void ActionTimelineWidget::selectAction(const QString& id) {
    for (const auto& seg : m_actions) if (seg.id == id) emit segmentSelected(seg);

    m_selectedActionId = id;
    if (m_canvas) m_canvas->setSelectedActionId(id);

    if (id.isEmpty()) {
        resetInputBarToAddMode();
        return;
    }

    for (const auto& a : m_actions) {
        if (a.id == id) {
            populateInputBarForEdit(a);
            break;
        }
    }
}

void ActionTimelineWidget::selectActorByPath(const QString& actorPath) {
    if (actorPath.isEmpty()) return;

    // Highlight actor on canvas
    if (m_canvas) m_canvas->setHighlightedActorPath(actorPath);

    // Sync actor combo
    if (m_actorCombo) {
        const int idx = m_actorCombo->findData(actorPath);
        if (idx >= 0) {
            QSignalBlocker b(m_actorCombo);
            m_actorCombo->setCurrentIndex(idx);
        }
    }

    // Make sure input bar is open so user can edit right away
    if (m_inputBar && !m_inputBar->isVisible()) {
        m_inputBar->setVisible(true);
    }

    // Check if there is an action for this actor at currentTime or first action
    QString targetActionId;
    for (const auto& a : m_actions) {
        if (a.actorPath == actorPath) {
            if (m_currentTimeS >= a.startS && m_currentTimeS <= a.endS) {
                targetActionId = a.id;
                break;
            }
            if (targetActionId.isEmpty()) {
                targetActionId = a.id;
            }
        }
    }

    if (!targetActionId.isEmpty()) {
        selectAction(targetActionId);
    } else {
        // No action yet for this actor, prepare Add Mode with this actor selected
        resetInputBarToAddMode();
        if (m_startSpin) m_startSpin->setValue(m_currentTimeS);
        if (m_endSpin) m_endSpin->setValue(std::min(m_durationS, m_currentTimeS + 5.0));
        QString label = m_actorCombo ? m_actorCombo->currentText() : "Actor";
        label.remove("👤 ");
        label.remove("🚗 ");
        label.remove("📦 ");
        if (m_modeLabel) {
            m_modeLabel->setText(QString("➕ New for: <b>%1</b>").arg(label));
        }
    }

    emit actorSelected(actorPath);
}

void ActionTimelineWidget::updateActorList(const QVector<UsdPrimItem>& prims) {
    const QString previousTarget = m_targetCombo->currentData().toString();
    m_targetCombo->clear();
    m_targetCombo->addItem("No target", QString());
    for (const auto& prim : prims) {
        if (prim.path.startsWith("/World/Camera") || prim.path.startsWith("/World/Paths") || prim.path == "/World/Paint" || prim.typeName == "Camera") continue;
        m_targetCombo->addItem(prim.actorLabel.isEmpty() ? prim.name : prim.actorLabel, prim.actorId.isEmpty() ? prim.name : prim.actorId);
    }
    m_targetCombo->setCurrentIndex(qMax(0, m_targetCombo->findData(previousTarget)));
    if (!m_actorCombo) return;
    const QString prevSelected = m_actorCombo->currentData().toString();
    m_actorCombo->clear();
    m_actorCombo->addItem("Select an asset…", QString());

    for (const auto& item : prims) {
        bool hasAuthoredBehavior = false;
        for (const auto& action : m_actions) {
            if (action.actorPath == item.path) {
                hasAuthoredBehavior = true;
                break;
            }
        }
        if (item.assetId.isEmpty() && item.actorId.isEmpty() && !hasAuthoredBehavior) {
            continue;
        }
        const QString displayName = !item.actorLabel.isEmpty() ? item.actorLabel :
                                    (!item.actorId.isEmpty() ? item.actorId : item.name);
        const QString icon = (item.typeName == "SkelRoot" || item.assetId.contains("human")) ? "👤 " :
                             (item.assetId.contains("car") || item.name.contains("Mustang") || item.name.contains("Pontiac") ? "🚗 " : "📦 ");
        m_actorCombo->addItem(icon + displayName, item.path);
        m_actorCombo->setItemData(m_actorCombo->count() - 1, item.identityColor, Qt::UserRole + 1);
    }

    if (!prevSelected.isEmpty()) {
        const int idx = m_actorCombo->findData(prevSelected);
        if (idx >= 0) m_actorCombo->setCurrentIndex(idx);
    }
}

QString ActionTimelineWidget::generateSummaryPrompt() const {
    if (m_actions.isEmpty()) return QString();

    QHash<QString, QVector<ActionSegment>> byActor;
    QHash<QString, QString> labels;
    for (const auto& a : m_actions) {
        if (a.generated) continue;
        byActor[a.actorPath].append(a);
        labels[a.actorPath] = a.actorLabel.isEmpty() ? a.actorPath.section('/', -1) : a.actorLabel;
    }

    QStringList lines;
    for (auto it = byActor.begin(); it != byActor.end(); ++it) {
        const QString actorPath = it.key();
        auto list = it.value();
        std::sort(list.begin(), list.end(), [](const ActionSegment& a, const ActionSegment& b) {
            return a.startS < b.startS;
        });

        QStringList actionPhrases;
        for (const auto& seg : list) {
            actionPhrases.append(QString("segment %1 (%2): %3").arg(seg.id, seg.lock.toUpper(), seg.action));
        }
        lines.append(QString("%1: %2.").arg(labels.value(actorPath), actionPhrases.join(", ")));
    }

    return lines.join("\n");
}

QJsonArray ActionTimelineWidget::toJson() const {
    QJsonArray arr;
    for (const auto& a : m_actions) {
        arr.append(a.toJson());
    }
    return arr;
}

void ActionTimelineWidget::fromJson(const QJsonArray& arr) {
    QVector<ActionSegment> loaded;
    for (const auto& val : arr) {
        if (val.isObject()) {
            loaded.append(ActionSegment::fromJson(val.toObject()));
        }
    }
    setActions(loaded);
    resetInputBarToAddMode();
}

void ActionTimelineWidget::setCollapsed(bool collapsed) {
    m_collapsed = collapsed;
    if (m_inputBar) m_inputBar->setVisible(!m_collapsed);
    if (m_advancedBar) m_advancedBar->setVisible(!m_collapsed && m_advancedExpanded);
    if (m_canvas) m_canvas->setVisible(!m_collapsed);
    if (m_toggleCollapseButton) {
        m_toggleCollapseButton->setText(m_collapsed ? "▼" : "▲");
    }
}

void ActionTimelineWidget::toggleCollapse() {
    setCollapsed(!m_collapsed);
}

// -------------------------------------------------------------
// Undo / Redo Implementation
// -------------------------------------------------------------
void ActionTimelineWidget::pushUndoState(const QString& description) {
    TimelineSnapshot snap;
    snap.actions = m_actions;
    snap.durationS = m_durationS;
    snap.description = description;

    m_undoStack.append(snap);
    if (m_undoStack.size() > 50) {
        m_undoStack.removeFirst();
    }
    m_redoStack.clear();
    updateUndoRedoButtons();
    emit undoSnapshotPushed();
    emit undoStateChanged();
}

bool ActionTimelineWidget::undo() {
    if (m_undoStack.isEmpty()) return false;

    TimelineSnapshot currentSnap;
    currentSnap.actions = m_actions;
    currentSnap.durationS = m_durationS;
    m_redoStack.append(currentSnap);

    TimelineSnapshot prev = m_undoStack.takeLast();
    m_actions = prev.actions;
    m_durationS = prev.durationS;

    if (m_durationSpin) {
        QSignalBlocker b(m_durationSpin);
        m_durationSpin->setValue(m_durationS);
    }
    m_selectedActionId.clear();
    if (m_canvas) {
        m_canvas->setDuration(m_durationS);
        m_canvas->setSelectedActionId(QString());
        setActions(m_actions);
    }

    resetInputBarToAddMode();
    updateTimeLabel();
    updateUndoRedoButtons();
    emit durationChanged(m_durationS);
    emit actionsChanged();
    emit undoStateChanged();
    return true;
}

bool ActionTimelineWidget::redo() {
    if (m_redoStack.isEmpty()) return false;

    TimelineSnapshot currentSnap;
    currentSnap.actions = m_actions;
    currentSnap.durationS = m_durationS;
    m_undoStack.append(currentSnap);

    TimelineSnapshot next = m_redoStack.takeLast();
    m_actions = next.actions;
    m_durationS = next.durationS;

    if (m_durationSpin) {
        QSignalBlocker b(m_durationSpin);
        m_durationSpin->setValue(m_durationS);
    }
    m_selectedActionId.clear();
    if (m_canvas) {
        m_canvas->setDuration(m_durationS);
        m_canvas->setSelectedActionId(QString());
        setActions(m_actions);
    }

    resetInputBarToAddMode();
    updateTimeLabel();
    updateUndoRedoButtons();
    emit durationChanged(m_durationS);
    emit actionsChanged();
    emit undoStateChanged();
    return true;
}

void ActionTimelineWidget::clearUndoHistory() {
    m_undoStack.clear();
    m_redoStack.clear();
    updateUndoRedoButtons();
    emit undoStateChanged();
}

void ActionTimelineWidget::updateUndoRedoButtons() {
    if (m_undoButton) {
        m_undoButton->setEnabled(canUndo());
        m_undoButton->setText(canUndo() ? QString("↶ Undo (%1)").arg(m_undoStack.size()) : "↶ Undo");
    }
    if (m_redoButton) {
        m_redoButton->setEnabled(canRedo());
        m_redoButton->setText(canRedo() ? QString("↷ Redo (%1)").arg(m_redoStack.size()) : "↷ Redo");
    }
}

// -------------------------------------------------------------
// Interactive Editing Slots
// -------------------------------------------------------------
void ActionTimelineWidget::handleAddOrUpdateButtonClicked() {
    if (selectedSegment().generated) { pinGeneratedAction(m_selectedActionId); return; }
    if (!selectedSegment().groupId.isEmpty()) { editSharedAction(); return; }
    if (!m_actorCombo || !m_actionCombo) return;

    const QString actorPath = m_actorCombo->currentData().toString();
    if (actorPath.isEmpty()) return;

    QString actorLabel = m_actorCombo->currentText();
    actorLabel.remove("👤 ");
    actorLabel.remove("🚗 ");
    actorLabel.remove("📦 ");

    const double startS = m_startSpin ? m_startSpin->value() : 0.0;
    const double endS = m_endSpin ? m_endSpin->value() : 5.0;
    if (endS <= startS) return;

    const QString action = m_actionCombo->currentText().trimmed();
    if (action.isEmpty()) return;

    const QColor col = m_actorCombo->currentData(Qt::UserRole + 1).value<QColor>();

    if (!m_selectedActionId.isEmpty()) {
        // UPDATE MODE
        for (auto& seg : m_actions) {
            if (seg.id == m_selectedActionId) {
                pushUndoState(QString("Update Action: %1").arg(action));
                seg.actorPath = actorPath;
                seg.actorLabel = actorLabel;
                seg.startS = startS;
                seg.endS = endS;
                seg.action = action;
        seg.lock = m_lockCombo->currentText();
        seg.speedProfile = m_speedCombo->currentText();
        seg.note = m_noteEdit->text();
        seg.toleranceS = m_toleranceSpin->value(); seg.flexible = seg.toleranceS > 0;
        seg.target = m_targetCombo->currentData().toString();
                seg.color = col.isValid() ? col : QColor("#58a6ff");

                if (endS > m_durationS) {
                    setShotDuration(std::ceil(endS));
                }
                setActions(m_actions);
                selectAction(seg.id);
                emit actionsChanged();
                break;
            }
        }
    } else {
        // ADD MODE
        ActionSegment seg;
        seg.id = QString("act_%1").arg(QDateTime::currentMSecsSinceEpoch());
        seg.actorPath = actorPath;
        seg.actorLabel = actorLabel;
        seg.startS = startS;
        seg.endS = endS;
        seg.action = action;
        seg.lock = m_lockCombo->currentText();
        seg.speedProfile = m_speedCombo->currentText();
        seg.note = m_noteEdit->text();
        seg.toleranceS = m_toleranceSpin->value(); seg.flexible = seg.toleranceS > 0;
        seg.target = m_targetCombo->currentData().toString();
        seg.color = col.isValid() ? col : QColor("#58a6ff");

        addAction(seg);

        // Advance start and end spinbox for convenience
        if (m_startSpin && m_endSpin) {
            const double nextStart = endS < m_durationS ? endS : 0.0;
            m_startSpin->setValue(nextStart);
            m_endSpin->setValue(std::min(m_durationS, nextStart + 5.0));
        }
    }
}

void ActionTimelineWidget::handleCancelEditClicked() {
    resetInputBarToAddMode();
    if (m_canvas) m_canvas->setSelectedActionId(QString());
}

void ActionTimelineWidget::handleDeleteSelectedClicked() {
    if (m_selectedActionId.isEmpty()) return;
    removeAction(m_selectedActionId);
}

void ActionTimelineWidget::handleActorComboChanged(int) {
    if (!m_actorCombo) return;
    const QString actorPath = m_actorCombo->currentData().toString();
    if (m_canvas) {
        m_canvas->setHighlightedActorPath(actorPath);
    }
}

void ActionTimelineWidget::handleCanvasActionSelected(const QString& id) {
    selectAction(id);
}

void ActionTimelineWidget::handleCanvasActorSelected(const QString& actorPath) {
    selectActorByPath(actorPath);
}

void ActionTimelineWidget::handleCanvasActionLiveUpdated(const ActionSegment& seg) {
    // Sync memory state and update spinboxes live during dragging
    for (auto& a : m_actions) {
        if (a.id == seg.id || (!seg.groupId.isEmpty() && a.groupId == seg.groupId)) {
            a.startS = seg.startS;
            a.endS = seg.endS;
        }
    }

    if (m_selectedActionId == seg.id) {
        if (m_startSpin) {
            QSignalBlocker b(m_startSpin);
            m_startSpin->setValue(seg.startS);
        }
        if (m_endSpin) {
            QSignalBlocker b(m_endSpin);
            m_endSpin->setValue(seg.endS);
        }
        if (m_modeLabel) {
            m_modeLabel->setText(QString("✏️ %1 (%2s – %3s)").arg(seg.actorLabel).arg(seg.startS, 0, 'f', 1).arg(seg.endS, 0, 'f', 1));
        }
    }
}

void ActionTimelineWidget::handleCanvasActionEditFinished() {
    // Check if any action exceeds duration
    double maxEnd = m_durationS;
    for (const auto& a : m_actions) {
        if (a.endS > maxEnd) maxEnd = a.endS;
    }
    if (maxEnd > m_durationS) {
        setShotDuration(std::ceil(maxEnd));
    }
    setActions(m_actions);
    emit actionsChanged();
}

void ActionTimelineWidget::keyPressEvent(QKeyEvent* event) {
    if (event->matches(QKeySequence::Undo)) {
        undo();
        event->accept();
        return;
    }
    if (event->matches(QKeySequence::Redo)) {
        redo();
        event->accept();
        return;
    }
    QWidget::keyPressEvent(event);
}

void ActionTimelineWidget::resetInputBarToAddMode() {
    m_selectedActionId.clear();
    if (m_startSpin) { m_startSpin->setReadOnly(false); m_startSpin->setButtonSymbols(QAbstractSpinBox::UpDownArrows); }
    if (m_endSpin) {
        m_endSpin->setReadOnly(false);
        m_endSpin->setButtonSymbols(QAbstractSpinBox::UpDownArrows);
        m_endSpin->setToolTip(QString());
    }

    if (m_modeLabel) {
        m_modeLabel->setText("➕ Add Mode");
        m_modeLabel->setStyleSheet("padding: 2px 6px; border-radius: 4px; font-weight: 600; font-size: 11px; color: #8b949e; background: #161b22; border: 1px solid #30363d;");
    }
    if (m_addButton) {
        m_addButton->setText("+ Add Behavior");
        m_addButton->setObjectName("primaryActionBtn");
        m_addButton->setStyleSheet("");
    }
    if (m_cancelEditButton) m_cancelEditButton->setVisible(false);
    if (m_deleteActionButton) m_deleteActionButton->setVisible(false);
}

void ActionTimelineWidget::populateInputBarForEdit(const ActionSegment& seg) {
    if (m_inputBar && !m_inputBar->isVisible()) {
        m_inputBar->setVisible(true);
    }

    if (m_actorCombo) {
        const int idx = m_actorCombo->findData(seg.actorPath);
        if (idx >= 0) {
            QSignalBlocker b(m_actorCombo);
            m_actorCombo->setCurrentIndex(idx);
        }
    }

    if (m_canvas) {
        m_canvas->setHighlightedActorPath(seg.actorPath);
    }

    if (m_startSpin) {
        QSignalBlocker b(m_startSpin);
        m_startSpin->setValue(seg.startS);
        m_startSpin->setReadOnly(seg.generated);
        m_startSpin->setButtonSymbols(seg.generated ? QAbstractSpinBox::NoButtons : QAbstractSpinBox::UpDownArrows);
    }
    if (m_endSpin) {
        QSignalBlocker b(m_endSpin);
        m_endSpin->setValue(seg.endS);
        m_endSpin->setReadOnly(seg.autoTiming || seg.generated);
        m_endSpin->setButtonSymbols(seg.autoTiming || seg.generated ? QAbstractSpinBox::NoButtons : QAbstractSpinBox::UpDownArrows);
        m_endSpin->setToolTip(seg.generated ? "Pin this AI action before editing it" :
                              seg.autoTiming ? "Calculated from drawn distance and natural movement speed" : QString());
    }
    if (m_actionCombo) {
        QSignalBlocker b(m_actionCombo);
        if (m_actionCombo->findText(seg.action) < 0) m_actionCombo->addItem(seg.action);
        m_actionCombo->setCurrentText(seg.action);
        m_lockCombo->setCurrentText(seg.lock);
        m_speedCombo->setCurrentText(seg.speedProfile);
        m_noteEdit->setText(seg.note); m_toleranceSpin->setValue(seg.flexible ? seg.toleranceS : 0);
        m_targetCombo->setCurrentIndex(m_targetCombo->findData(seg.target));
    }

    if (m_modeLabel) {
        m_modeLabel->setText(QString("✏️ Editing: <b>%1</b> (%2s – %3s)").arg(seg.actorLabel).arg(seg.startS, 0, 'f', 1).arg(seg.endS, 0, 'f', 1));
        m_modeLabel->setStyleSheet("padding: 2px 6px; border-radius: 4px; font-weight: 600; font-size: 11px; color: #58a6ff; background: #0c2d6b; border: 1px solid #1f6feb;");
    }

    if (m_addButton) {
        m_addButton->setText(seg.generated ? "Pin AI Action" : seg.groupId.isEmpty() ? "✓ Update Segment" : "Edit Shared Action");
        m_addButton->setObjectName("updateActionBtn");
        m_addButton->setStyleSheet("background: #238636; border-color: #2ea043; color: #ffffff;");
    }
    if (m_cancelEditButton) m_cancelEditButton->setVisible(true);
    if (m_deleteActionButton) m_deleteActionButton->setVisible(!seg.generated);
}

void ActionTimelineWidget::updateTimeLabel() {
    if (!m_timeLabel) return;
    m_timeLabel->setText(QString("⏱ %1s / %2s")
                             .arg(m_currentTimeS, 4, 'f', 1, QChar('0'))
                             .arg(m_durationS, 4, 'f', 1, QChar('0')));
}

ActionSegment ActionTimelineWidget::selectedSegment() const {
    for (const auto& seg : m_actions) if (seg.id == m_selectedActionId) return seg;
    return {};
}
void ActionTimelineWidget::setAvailableActions(const QStringList& verbs) {
    const QString selected = m_actionCombo->currentText();
    m_actionCombo->clear(); m_actionCombo->addItems(verbs);
    if (verbs.contains(selected)) m_actionCombo->setCurrentText(selected);
}
void ActionTimelineWidget::setCameraRange(double start, double end, const QString& label) {
    // Render-only items are excluded from toJson()/director actor persistence by MainWindow.
    const QString id = "render_only_camera";
    for (auto& seg : m_actions) if (seg.id == id) { seg.startS = start; seg.endS = end; seg.action = "render only: " + label; setActions(m_actions); return; }
    ActionSegment camera; camera.id = id; camera.actorPath = "/World/Cameras/active";
    camera.actorLabel = "Camera · render only"; camera.action = "render only: " + label;
    camera.startS = start; camera.endS = end; camera.color = QColor("#38bdf8");
    m_actions.append(camera); setActions(m_actions);
}

void ActionTimelineWidget::setPickedTarget(const QString& target) {
    int index=m_targetCombo->findData(target);if(index<0){m_targetCombo->addItem(target,target);index=m_targetCombo->count()-1;}
    m_targetCombo->setCurrentIndex(index);
    auto segment=selectedSegment();if(!segment.id.isEmpty()){segment.target=target;updateAction(segment);}
}
