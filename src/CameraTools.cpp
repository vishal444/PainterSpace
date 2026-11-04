#include "CameraTools.h"
#include "UsdViewportWidget.h"

#include <QKeyEvent>
#include <QMouseEvent>
#include <QWheelEvent>

CameraToolController::CameraToolController(UsdViewportWidget* viewport)
    : QObject(viewport), myViewport(viewport) {}

CameraToolController::~CameraToolController() = default;

void CameraToolController::setTool(CameraToolType tool) {
    if (myTool == tool) return;
    myTool = tool;
    myIsDrawing = false;
    myIsAiming = false;
    myActiveStroke.clear();
    emit toolChanged(myTool);
}

void CameraToolController::setRigData(const CameraRigData& rig) {
    myRig = rig;
    if (mySelectedPointIndex >= myRig.points.size()) {
        mySelectedPointIndex = myRig.points.isEmpty() ? -1 : 0;
    }
    emit rigChanged();
}

bool CameraToolController::intersectDrawingPlane(const QPointF& screenPt, QVector3D& outWorldPt) const {
    if (!myViewport) return false;
    double h = myDrawingPlaneHeight;
    if (mySelectedPointIndex >= 0 && mySelectedPointIndex < myRig.points.size()) {
        h = myRig.points[mySelectedPointIndex].position.y();
    } else if (!myRig.points.isEmpty()) {
        h = myRig.points.first().position.y();
    }
    return myViewport->screenToGroundPlane(screenPt, h, outWorldPt);
}

bool CameraToolController::handleMousePress(QMouseEvent* event) {
    if (myTool == CameraToolType::None) return false;

    myLastMousePos = event->position();

    if (myTool == CameraToolType::DrawPath && event->button() == Qt::LeftButton) {
        QVector3D hitPt;
        if (intersectDrawingPlane(event->position(), hitPt)) {
            if (myViewport) {
                myViewport->pushUndoSnapshot();
            }
            myIsDrawing = true;
            myActiveStroke.clear();
            myActiveStroke.append(hitPt);
            return true;
        }
        return true;
    } else if (myTool == CameraToolType::LookAt && event->button() == Qt::LeftButton) {
        QString hitPrim;
        QVector3D hitPt;
        if (myViewport->tryCpuRayPick(event->position(), hitPrim, &hitPt)) {
            if (myViewport) {
                myViewport->pushUndoSnapshot();
            }
            myRig.aimTargets = {hitPrim};
            myRig.aimMode = CameraAimMode::LookAt;
            emit rigChanged();
            return true;
        }
    } else if (myTool == CameraToolType::Frame && event->button() == Qt::LeftButton) {
        QString hitPrim;
        QVector3D hitPt;
        if (myViewport->tryCpuRayPick(event->position(), hitPrim, &hitPt)) {
            if (myViewport) {
                myViewport->pushUndoSnapshot();
            }
            myRig.shotSubject = hitPrim;
            myRig.aimTargets = {hitPrim};
            myRig.aimMode = CameraAimMode::Frame;
            emit rigChanged();
            return true;
        }
    } else if (myTool == CameraToolType::Aim && event->button() == Qt::LeftButton) {
        if (myViewport) {
            myViewport->pushUndoSnapshot();
        }
        myIsAiming = true;
        return true;
    } else if (myTool == CameraToolType::EditPath && event->button() == Qt::LeftButton) {
        // Test hit on control points
        int hitIdx = -1;
        double closestDist = 18.0;
        for (int i = 0; i < myRig.points.size(); ++i) {
            bool inFront = false;
            // Project point using viewport
            QPointF sc = myViewport->projectWorldToScreen(myRig.points[i].position,
                                                          myViewport->currentProjectionMatrix(static_cast<double>(myViewport->width()) / std::max(1, myViewport->height())) * myViewport->currentViewMatrix(),
                                                          &inFront);
            if (inFront) {
                const double d = (event->position() - sc).manhattanLength();
                if (d < closestDist) {
                    closestDist = d;
                    hitIdx = i;
                }
            }
        }
        if (hitIdx >= 0) {
            if (myViewport) {
                myViewport->pushUndoSnapshot();
            }
            mySelectedPointIndex = hitIdx;
            emit pointSelectionChanged(mySelectedPointIndex);
            return true;
        }
    }
    return false;
}

bool CameraToolController::handleMouseMove(QMouseEvent* event) {
    if (myTool == CameraToolType::None) return false;

    const QPointF delta = event->position() - myLastMousePos;
    myLastMousePos = event->position();

    if (myTool == CameraToolType::DrawPath && myIsDrawing && (event->buttons() & Qt::LeftButton)) {
        QVector3D hitPt;
        if (intersectDrawingPlane(event->position(), hitPt)) {
            if (myActiveStroke.isEmpty() || (hitPt - myActiveStroke.last()).length() > 0.15f) {
                myActiveStroke.append(hitPt);
                return true;
            }
        }
    } else if (myTool == CameraToolType::Aim && myIsAiming && (event->buttons() & Qt::LeftButton)) {
        // Pan and tilt
        const float pitchDelta = static_cast<float>(-delta.y() * 0.2);
        const float yawDelta = static_cast<float>(-delta.x() * 0.2);
        myRig.aimOffset += QVector3D(yawDelta * 0.05f, pitchDelta * 0.05f, 0.0f);
        emit rigChanged();
        return true;
    } else if (myTool == CameraToolType::EditPath && mySelectedPointIndex >= 0 && (event->buttons() & Qt::LeftButton)) {
        QVector3D hitPt;
        if (intersectDrawingPlane(event->position(), hitPt)) {
            if (mySelectedPointIndex < myRig.points.size()) {
                myRig.points[mySelectedPointIndex].position = hitPt;
                emit rigChanged();
                return true;
            }
        }
    }
    return false;
}

bool CameraToolController::handleMouseRelease(QMouseEvent* event) {
    if (myTool == CameraToolType::None) return false;

    if (myTool == CameraToolType::DrawPath && myIsDrawing) {
        myIsDrawing = false;
        if (myActiveStroke.size() >= 2) {
            QVector<QVector3D> simplified = simplifyPointsRdp(myActiveStroke, 0.1);
            QVector3D startPos;
            if (!myRig.points.isEmpty()) {
                startPos = myRig.points.first().position;
            } else if (myViewport) {
                startPos = myViewport->cameraEvaluatedPosition();
            } else {
                startPos = QVector3D(0.0f, 1.6f, 5.0f);
            }

            myRig.points.clear();
            CameraRigPoint p0;
            p0.position = startPos;
            p0.focalLength = myRig.focalStart;
            myRig.points.append(p0);

            // Connect stroke points from startPos towards endpoint
            const int skipFirst = (!simplified.isEmpty() && (simplified.first() - startPos).length() < 0.35f) ? 1 : 0;
            for (int i = skipFirst; i < simplified.size(); ++i) {
                CameraRigPoint rp;
                rp.position = simplified[i];
                rp.focalLength = myRig.focalEnd;
                myRig.points.append(rp);
            }
            myRig.pathDrawn = myRig.points.size() >= 2;

            if (myRig.pathEndS <= myRig.pathStartS) {
                myRig.pathEndS = myRig.pathStartS + 5.0;
            }

            myActiveStroke.clear();
            mySelectedPointIndex = 0; // Keep camera at start position (Joint 1)
            emit rigChanged();
            emit pointSelectionChanged(mySelectedPointIndex);
            return true;
        }
        myActiveStroke.clear();
    } else if (myTool == CameraToolType::Aim && myIsAiming) {
        myIsAiming = false;
        return true;
    }
    return false;
}

bool CameraToolController::handleWheel(QWheelEvent* event) {
    if (myTool == CameraToolType::DrawPath && (event->modifiers() & Qt::ShiftModifier)) {
        const double delta = event->angleDelta().y() > 0 ? 0.2 : -0.2;
        myDrawingPlaneHeight = std::clamp(myDrawingPlaneHeight + delta, -20.0, 50.0);
        return true;
    }
    return false;
}

bool CameraToolController::handleKeyPress(QKeyEvent* event) {
    if (event->key() == Qt::Key_Delete || event->key() == Qt::Key_Backspace) {
        if (myTool == CameraToolType::EditPath && mySelectedPointIndex >= 0) {
            removeSelectedPoint();
            return true;
        }
    }
    return false;
}

void CameraToolController::insertPointAt(int index, const QVector3D& pos) {
    CameraRigPoint rp;
    rp.position = pos;
    const int idx = std::clamp(index, 0, static_cast<int>(myRig.points.size()));
    myRig.points.insert(idx, rp);
    mySelectedPointIndex = idx;
    emit rigChanged();
    emit pointSelectionChanged(mySelectedPointIndex);
}

void CameraToolController::removeSelectedPoint() {
    if (mySelectedPointIndex >= 0 && mySelectedPointIndex < myRig.points.size()) {
        if (myViewport) {
            myViewport->pushUndoSnapshot();
        }
        myRig.points.removeAt(mySelectedPointIndex);
        if (mySelectedPointIndex >= myRig.points.size()) {
            mySelectedPointIndex = static_cast<int>(myRig.points.size()) - 1;
        }
        emit rigChanged();
        emit pointSelectionChanged(mySelectedPointIndex);
    }
}

void CameraToolController::clearPath() {
    if (myViewport) {
        myViewport->pushUndoSnapshot();
    }
    QVector3D camPos;
    if (!myRig.points.isEmpty()) {
        camPos = myRig.points.first().position;
    } else if (myViewport) {
        camPos = myViewport->cameraEvaluatedPosition();
    } else {
        camPos = QVector3D(0.0f, 1.6f, 5.0f);
    }
    myRig.points.clear();
    myRig.pathDrawn = false;
    CameraRigPoint p0;
    p0.position = camPos;
    p0.focalLength = myRig.focalStart;
    myRig.points.append(p0);
    mySelectedPointIndex = -1;
    emit rigChanged();
    emit pointSelectionChanged(mySelectedPointIndex);
}
