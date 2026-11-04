#pragma once

#include <QColor>
#include <QHash>
#include <QJsonArray>
#include <QJsonObject>
#include <QString>
#include <QVector>
#include <QWidget>

#include "UsdViewportWidget.h"

class QLabel;
class QLineEdit;
class QComboBox;
class QDoubleSpinBox;
class QPushButton;
class QScrollArea;

struct ActionSegment {
    QString id;
    QString actorPath;    // USD prim path e.g. "/World/Hero"
    QString actorLabel;   // Display label e.g. "Hero"
    double startS = 0.0;
    double endS = 5.0;
    QString action;
    QString activity;      // Specific activity for a shared play action.
    QString groupId;       // Shared event identity; empty for an individual action.
    QStringList participants; // Actor paths selected for the shared event.
    bool generated = false; // Displayed from the compiled plan; not authored in the brief.
    QString sourceBeatId;
    QString lock = "locked";
    QString target;
    QString pathId;
    QString note;
    QString speedProfile = "constant";
    double distanceM = -1.0; // Derived from a drawn route or target, when available.
    bool autoTiming = false; // Walk/run duration follows distance instead of the end-time input.
    bool flexible = false;
    double toleranceS = 0;
    QColor color = QColor("#58a6ff");

    QJsonObject toJson() const;
    static ActionSegment fromJson(const QJsonObject& obj);
};

class ActionTimelineCanvas : public QWidget {
    Q_OBJECT

public:
    enum class HitZone {
        None,
        HeaderActor,
        Ruler,
        BlockBody,
        BlockLeftEdge,
        BlockRightEdge
    };

    struct HitResult {
        HitZone zone = HitZone::None;
        QString actionId;
        QString actorPath;
    };

    enum class DragMode {
        None,
        Scrub,
        MoveSegment,
        ResizeStart,
        ResizeEnd
    };

    explicit ActionTimelineCanvas(QWidget* parent = nullptr);

    void setDuration(double durationS);
    double duration() const { return m_durationS; }

    void setCurrentTime(double timeS);
    double currentTime() const { return m_currentTimeS; }

    void setActions(const QVector<ActionSegment>& actions);
    const QVector<ActionSegment>& actions() const { return m_actions; }

    void setSelectedActionId(const QString& id);
    QString selectedActionId() const { return m_selectedActionId; }

    void setHighlightedActorPath(const QString& actorPath);
    QString highlightedActorPath() const { return m_highlightedActorPath; }

signals:
    void timeScrubbed(double timeS);
    void actionSelected(const QString& id);
    void actorSelected(const QString& actorPath);
    void actionDeleteRequested(const QString& id);
    void actionDuplicateRequested(const QString& id);
    void actionPinRequested(const QString& id);
    void actionLiveUpdated(const ActionSegment& seg);
    void actionEditStarted();
    void actionEditFinished();
    void undoRequested();
    void redoRequested();

protected:
    void paintEvent(QPaintEvent* event) override;
    void mousePressEvent(QMouseEvent* event) override;
    void mouseMoveEvent(QMouseEvent* event) override;
    void mouseReleaseEvent(QMouseEvent* event) override;
    void mouseDoubleClickEvent(QMouseEvent* event) override;
    void keyPressEvent(QKeyEvent* event) override;

private:
    double m_durationS = 15.0;
    double m_currentTimeS = 0.0;
    QVector<ActionSegment> m_actions;
    QString m_selectedActionId;
    QString m_highlightedActorPath;

    // Interactive Dragging & Resizing
    DragMode m_dragMode = DragMode::None;
    QString m_dragActionId;
    double m_dragInitialMouseTime = 0.0;
    double m_dragInitialStartS = 0.0;
    double m_dragInitialEndS = 0.0;
    bool m_hasDragged = false;

    double timeFromX(double x) const;
    double xFromTime(double timeS) const;
    HitResult hitTest(const QPoint& pt) const;
};

class ActionTimelineWidget : public QWidget {
    Q_OBJECT

public:
    struct TimelineSnapshot {
        QVector<ActionSegment> actions;
        double durationS = 15.0;
        QString description;
    };

    explicit ActionTimelineWidget(QWidget* parent = nullptr);

    void setShotDuration(double seconds);
    double shotDuration() const;

    void setCurrentTime(double seconds);
    double currentTime() const { return m_currentTimeS; }

    void setActions(const QVector<ActionSegment>& actions);
    QVector<ActionSegment> actions() const { return m_actions; }
    void setMovementDistances(const QHash<QString, double>& distances, bool recordUndo = true);

    void addAction(const ActionSegment& action);
    void updateAction(const ActionSegment& action);
    void removeAction(const QString& id);
    void clearActions();
    void duplicateAction(const QString& id);
    void pinGeneratedAction(const QString& id);
    void editSharedAction();

    void selectAction(const QString& id);
    void selectActorByPath(const QString& actorPath);
    void updateActorList(const QVector<UsdPrimItem>& prims);

    QString generateSummaryPrompt() const;
    ActionSegment selectedSegment() const;
    void setAvailableActions(const QStringList& verbs);
    void setCameraRange(double start, double end, const QString& label);
    void setPickedTarget(const QString& target);

    QJsonArray toJson() const;
    void fromJson(const QJsonArray& arr);

    bool isCollapsed() const { return m_collapsed; }
    void setCollapsed(bool collapsed);

    // Undo / Redo
    void pushUndoState(const QString& description = QString());
    bool undo();
    bool redo();
    bool canUndo() const { return !m_undoStack.isEmpty(); }
    bool canRedo() const { return !m_redoStack.isEmpty(); }
    void clearUndoHistory();

signals:
    void actionsChanged();
    void targetPickRequested();
    void drawSegmentPathRequested(const QString& actorPath, const QString& pathId);
    void segmentSelected(const ActionSegment& segment);
    void cameraRangeChanged(double start, double end);
    void timeScrubbed(double seconds);
    void syncPromptRequested(const QString& summary);
    void durationChanged(double seconds);
    void actorSelected(const QString& actorPath);
    void undoSnapshotPushed();
    void undoStateChanged();

private slots:
    void handleAddOrUpdateButtonClicked();
    void handleCancelEditClicked();
    void handleDeleteSelectedClicked();
    void handleActorComboChanged(int index);
    void toggleCollapse();

    void handleCanvasActionSelected(const QString& id);
    void handleCanvasActorSelected(const QString& actorPath);
    void handleCanvasActionLiveUpdated(const ActionSegment& seg);
    void handleCanvasActionEditFinished();

protected:
    void keyPressEvent(QKeyEvent* event) override;

private:
    double m_durationS = 15.0;
    double m_currentTimeS = 0.0;
    bool m_collapsed = false;
    bool m_advancedExpanded = false;
    QVector<ActionSegment> m_actions;
    QString m_selectedActionId;

    // Undo / Redo history stacks
    QVector<TimelineSnapshot> m_undoStack;
    QVector<TimelineSnapshot> m_redoStack;

    // Header controls
    QLabel* m_titleLabel = nullptr;
    QLabel* m_timeLabel = nullptr;
    QDoubleSpinBox* m_durationSpin = nullptr;
    QPushButton* m_undoButton = nullptr;
    QPushButton* m_redoButton = nullptr;
    QPushButton* m_quickAddToggle = nullptr;
    QPushButton* m_syncPromptButton = nullptr;
    QPushButton* m_clearButton = nullptr;
    QPushButton* m_toggleCollapseButton = nullptr;

    // Quick Add / Edit controls
    QWidget* m_inputBar = nullptr;
    QWidget* m_advancedBar = nullptr;
    QLabel* m_modeLabel = nullptr;
    QComboBox* m_actorCombo = nullptr;
    QDoubleSpinBox* m_startSpin = nullptr;
    QDoubleSpinBox* m_endSpin = nullptr;
    QComboBox* m_actionCombo = nullptr;
    QLineEdit* m_noteEdit = nullptr;
    QDoubleSpinBox* m_toleranceSpin = nullptr;
    QComboBox* m_speedCombo = nullptr;
    QComboBox* m_lockCombo = nullptr;
    QComboBox* m_targetCombo = nullptr;
    QPushButton* m_addButton = nullptr;
    QPushButton* m_cancelEditButton = nullptr;
    QPushButton* m_deleteActionButton = nullptr;

    // Canvas
    ActionTimelineCanvas* m_canvas = nullptr;

    void updateTimeLabel();
    void updateUndoRedoButtons();
    void resetInputBarToAddMode();
    void populateInputBarForEdit(const ActionSegment& seg);
};
