#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "HAL/ThreadSafeCounter.h"
#include "PhysicsPublic.h"
#include "ADPPhysicsRuntimeDriver.generated.h"

class APhysicsConstraintActor;
class UPrimitiveComponent;
class USplineMeshComponent;

USTRUCT(BlueprintType)
struct ADPPHYSICSRUNTIME_API FADPDrivenBodyConfig
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FName BodyId;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	TObjectPtr<AActor> Actor = nullptr;

	UPROPERTY(Transient)
	TObjectPtr<UPrimitiveComponent> PrimitiveComponent = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	bool bDynamic = true;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	bool bSimulatePhysics = true;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	bool bEnableGravity = true;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	bool bCollisionEnabled = true;

	UPROPERTY(Transient)
	bool bPersistentForceDriven = false;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FName ColliderKind = NAME_None;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float MassKg = 1.0f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float LinearDamping = 0.15f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float AngularDamping = 0.25f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FVector InitialVelocityCmPerSec = FVector::ZeroVector;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FVector InitialImpulseKgCmPerSec = FVector::ZeroVector;
};

USTRUCT(BlueprintType)
struct ADPPHYSICSRUNTIME_API FADPContinuousForceConfig
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FName ForceId;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FName BodyId;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FVector ForceNewton = FVector::ZeroVector;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float StartTimeSeconds = 0.0f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float EndTimeSeconds = 0.0f;
};

USTRUCT(BlueprintType)
struct ADPPHYSICSRUNTIME_API FADPRadialForceConfig
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FName ForceId;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FName SourceBodyId;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FName TargetBodyId;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float StrengthNewtonMeterSquared = 0.0f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float SofteningDistanceMeters = 0.0f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float MaxDistanceMeters = 0.0f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float StartTimeSeconds = 0.0f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float EndTimeSeconds = 0.0f;

	FVector LastSourcePositionMeters = FVector::ZeroVector;
	FVector LastTargetPositionMeters = FVector::ZeroVector;
	FVector LastForceNewton = FVector::ZeroVector;
	float LastDistanceMeters = 0.0f;
	bool bLastApplied = false;
};

USTRUCT(BlueprintType)
struct ADPPHYSICSRUNTIME_API FADPAerodynamicWindConfig
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FName FieldId;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FName BodyId;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FVector VelocityMetersPerSecond = FVector::ZeroVector;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float AirDensityKgPerCubicMeter = 1.225f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float DragCoefficient = 1.0f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float GustAmplitude = 0.0f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float GustFrequencyHz = 0.0f;

	FVector LastRelativeVelocityMetersPerSecond = FVector::ZeroVector;
	FVector LastForceNewton = FVector::ZeroVector;
	float LastProjectedAreaSquareMeters = 0.0f;
	bool bLastApplied = false;
};

USTRUCT(BlueprintType)
struct ADPPHYSICSRUNTIME_API FADPCompliantContactConfig
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FName ContactId;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FName BodyAId;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FName BodyBId;

	UPROPERTY(Transient)
	TObjectPtr<UPrimitiveComponent> BodyAComponent = nullptr;

	UPROPERTY(Transient)
	TObjectPtr<UPrimitiveComponent> BodyBComponent = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float ActivationDistanceCm = 0.0f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float StiffnessNPerM = 0.0f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float DampingNsPerM = 0.0f;
};

USTRUCT(BlueprintType)
struct ADPPHYSICSRUNTIME_API FADPUnilateralDistanceSpringConfig
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FName ConstraintId;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FName BodyAId;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FName BodyBId;

	UPROPERTY(Transient)
	TObjectPtr<UPrimitiveComponent> BodyAComponent = nullptr;

	UPROPERTY(Transient)
	TObjectPtr<UPrimitiveComponent> BodyBComponent = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	bool bBodyAWorld = false;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	bool bBodyBWorld = false;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FVector FrameAPositionCm = FVector::ZeroVector;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FVector FrameBPositionCm = FVector::ZeroVector;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float RestLengthCm = 0.0f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float StiffnessNPerM = 0.0f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float DampingNsPerM = 0.0f;

	TSharedPtr<FThreadSafeCounter, ESPMode::ThreadSafe> EvaluationCount;
	TSharedPtr<FThreadSafeCounter, ESPMode::ThreadSafe> ActiveEvaluationCount;
	float LastDistanceCm = 0.0f;
	float LastExtensionCm = 0.0f;
	float LastSeparationSpeedCmPerSec = 0.0f;
	float LastTensionNewton = 0.0f;
	FVector LastDirectionAToB = FVector::ZeroVector;
	FVector LastForceOnBodyBNewton = FVector::ZeroVector;
};

USTRUCT(BlueprintType)
struct ADPPHYSICSRUNTIME_API FADPUnilateralPathLengthSpringConfig
{
	GENERATED_BODY()

	FName ConstraintId;
	FName BodyAId;
	FName BodyBId;
	TObjectPtr<UPrimitiveComponent> BodyAComponent = nullptr;
	TObjectPtr<UPrimitiveComponent> BodyBComponent = nullptr;
	FVector FrameAPositionCm = FVector::ZeroVector;
	FVector FrameBPositionCm = FVector::ZeroVector;
	FVector FixedWaypointCm = FVector::ZeroVector;
	float RestLengthCm = 0.0f;
	float StiffnessNPerM = 0.0f;
	float DampingNsPerM = 0.0f;
	TSharedPtr<FThreadSafeCounter, ESPMode::ThreadSafe> EvaluationCount;
	TSharedPtr<FThreadSafeCounter, ESPMode::ThreadSafe> ActiveEvaluationCount;
	float LastSegmentALengthCm = 0.0f;
	float LastSegmentBLengthCm = 0.0f;
	float LastPathLengthCm = 0.0f;
	float LastExtensionCm = 0.0f;
	float LastPathSpeedCmPerSec = 0.0f;
	float LastTensionNewton = 0.0f;
	FVector LastPointACm = FVector::ZeroVector;
	FVector LastPointBCm = FVector::ZeroVector;
	FVector LastDirectionAToWaypoint = FVector::ZeroVector;
	FVector LastDirectionBToWaypoint = FVector::ZeroVector;
	FVector LastForceOnBodyANewton = FVector::ZeroVector;
	FVector LastForceOnBodyBNewton = FVector::ZeroVector;
};

USTRUCT(BlueprintType)
struct ADPPHYSICSRUNTIME_API FADPTransformSample
{
	GENERATED_BODY()

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FName BodyId;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	int32 FrameIndex = 0;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	float TimeSeconds = 0.0f;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FVector LocationCm = FVector::ZeroVector;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FRotator RotationDegrees = FRotator::ZeroRotator;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FVector VelocityCmPerSec = FVector::ZeroVector;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FVector AngularVelocityRadPerSec = FVector::ZeroVector;
};

USTRUCT(BlueprintType)
struct ADPPHYSICSRUNTIME_API FADPContactSample
{
	GENERATED_BODY()

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	int32 FrameIndex = 0;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	float TimeSeconds = 0.0f;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FName BodyA;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FName BodyB;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	float GapCm = 0.0f;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FVector AxisGapsCm = FVector::ZeroVector;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	bool bNativeCollision = false;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	float NormalImpulseNs = 0.0f;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FVector ImpactPointCm = FVector::ZeroVector;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FVector ImpactNormal = FVector::ZeroVector;
};

USTRUCT(BlueprintType)
struct ADPPHYSICSRUNTIME_API FADPConstraintSample
{
	GENERATED_BODY()

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FName ConstraintId;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FVector TranslationCm = FVector::ZeroVector;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FVector RelativeVelocityCmPerSec = FVector::ZeroVector;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FVector LinearForceEngineUnits = FVector::ZeroVector;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FVector AngularTorqueEngineUnits = FVector::ZeroVector;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FVector PositionTargetCm = FVector::ZeroVector;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FVector Stiffness = FVector::ZeroVector;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	bool bUnilateralDistanceSpring = false;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	float DistanceSpringRestLengthCm = 0.0f;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	float DistanceSpringStiffnessNPerM = 0.0f;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	float DistanceSpringDampingNsPerM = 0.0f;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	float DistanceSpringDistanceCm = 0.0f;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	float DistanceSpringExtensionCm = 0.0f;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	float DistanceSpringSeparationSpeedCmPerSec = 0.0f;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	float DistanceSpringTensionNewton = 0.0f;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FVector DistanceSpringDirectionAToB = FVector::ZeroVector;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	FVector DistanceSpringForceOnBodyBNewton = FVector::ZeroVector;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	int32 DistanceSpringEvaluationCount = 0;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	int32 DistanceSpringActiveEvaluationCount = 0;

	bool bUnilateralPathLengthSpring = false;
	FVector PathSpringFixedWaypointCm = FVector::ZeroVector;
	float PathSpringRestLengthCm = 0.0f;
	float PathSpringStiffnessNPerM = 0.0f;
	float PathSpringDampingNsPerM = 0.0f;
	float PathSpringSegmentALengthCm = 0.0f;
	float PathSpringSegmentBLengthCm = 0.0f;
	float PathSpringPathLengthCm = 0.0f;
	float PathSpringExtensionCm = 0.0f;
	float PathSpringPathSpeedCmPerSec = 0.0f;
	float PathSpringTensionNewton = 0.0f;
	FVector PathSpringPointACm = FVector::ZeroVector;
	FVector PathSpringPointBCm = FVector::ZeroVector;
	FVector PathSpringDirectionAToWaypoint = FVector::ZeroVector;
	FVector PathSpringDirectionBToWaypoint = FVector::ZeroVector;
	FVector PathSpringForceOnBodyANewton = FVector::ZeroVector;
	FVector PathSpringForceOnBodyBNewton = FVector::ZeroVector;
	int32 PathSpringEvaluationCount = 0;
	int32 PathSpringActiveEvaluationCount = 0;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	bool bBroken = false;

	bool bTerminated = false;
	bool bValidConstraint = false;
};

struct FADPFrameCapture
{
	int32 FrameIndex = 0;
	float TimeSeconds = 0.0f;
	TArray<FADPTransformSample> Transforms;
	TArray<FADPContactSample> Contacts;
	TArray<FADPConstraintSample> Constraints;
	TArray<FADPContinuousForceConfig> ActiveForces;
	TArray<FADPRadialForceConfig> ActiveRadialForces;
	TArray<FADPAerodynamicWindConfig> ActiveWindForces;
};

UCLASS(BlueprintType, Blueprintable)
class ADPPHYSICSRUNTIME_API AADPPhysicsRuntimeDriver : public AActor
{
	GENERATED_BODY()

public:
	AADPPhysicsRuntimeDriver();

	virtual void Tick(float DeltaSeconds) override;

	UFUNCTION(BlueprintCallable, Category = "ADP Physics")
	void ResetDriver();

	UFUNCTION(BlueprintCallable, Category = "ADP Physics")
	void RegisterBody(
		FName BodyId,
		AActor* Actor,
		float MassKg,
		FVector InitialVelocityCmPerSec,
		FVector InitialImpulseKgCmPerSec,
		bool bEnableGravity,
		float LinearDamping,
		float AngularDamping,
		bool bSimulatePhysics);

	UFUNCTION(BlueprintCallable, Category = "ADP Physics")
	void RegisterBodyMeters(
		FName BodyId,
		AActor* Actor,
		float MassKg,
		FVector InitialVelocityMetersPerSecond,
		FVector InitialImpulseNewtonSeconds,
		bool bEnableGravity,
		float LinearDamping,
		float AngularDamping,
		bool bSimulatePhysics);

	UFUNCTION(BlueprintCallable, Category = "ADP Physics")
	void RegisterBodyMetersWithCollider(
		FName BodyId,
		AActor* Actor,
		FName ColliderKind,
		float MassKg,
		FVector InitialVelocityMetersPerSecond,
		FVector InitialImpulseNewtonSeconds,
		bool bEnableGravity,
		float LinearDamping,
		float AngularDamping,
		bool bSimulatePhysics,
		bool bCollisionEnabled);

	UFUNCTION(BlueprintCallable, Category = "ADP Physics")
	void RegisterStaticBody(FName BodyId, AActor* Actor);

	UFUNCTION(BlueprintCallable, Category = "ADP Physics")
	void RegisterStaticBodyWithCollider(FName BodyId, AActor* Actor, FName ColliderKind, bool bCollisionEnabled);

	UFUNCTION(BlueprintCallable, Category = "ADP Physics|Forces")
	bool RegisterContinuousForce(
		FName ForceId,
		FName BodyId,
		FVector ForceNewton,
		float StartTimeSeconds,
		float EndTimeSeconds);

	UFUNCTION(BlueprintCallable, Category = "ADP Physics|Forces")
	bool RegisterRadialForce(
		FName ForceId,
		FName SourceBodyId,
		FName TargetBodyId,
		float StrengthNewtonMeterSquared,
		float SofteningDistanceMeters,
		float MaxDistanceMeters,
		float StartTimeSeconds,
		float EndTimeSeconds);

	UFUNCTION(BlueprintCallable, Category = "ADP Physics|Forces")
	bool RegisterAerodynamicWind(
		FName FieldId,
		FName BodyId,
		FVector VelocityMetersPerSecond,
		float AirDensityKgPerCubicMeter,
		float DragCoefficient,
		float GustAmplitude,
		float GustFrequencyHz);

	UFUNCTION(BlueprintCallable, Category = "ADP Physics|Contacts")
	bool RegisterCompliantContact(
		FName ContactId,
		FName BodyAId,
		FName BodyBId,
		float ActivationDistanceMeters,
		float StiffnessNPerM,
		float DampingNsPerM);

	UFUNCTION(BlueprintCallable, Category = "ADP Physics")
	void StartCapture(float InSampleIntervalSeconds, int32 InMaxFrames, const FString& InOutputPath);

	UFUNCTION(BlueprintCallable, Category = "ADP Physics")
	bool PrepareCapture(float InSampleIntervalSeconds, int32 InMaxFrames, const FString& InOutputPath);

	UFUNCTION(BlueprintCallable, Category = "ADP Physics|Constraints")
	APhysicsConstraintActor* BindConstraint(
		FName ConstraintId,
		FName BodyAId,
		FName BodyBId,
		FVector FrameAPositionCm,
		FVector FrameAPrimaryAxis,
		FVector FrameASecondaryAxis,
		FVector FrameBPositionCm,
		FVector FrameBPrimaryAxis,
		FVector FrameBSecondaryAxis,
		FName LinearXMotion,
		FName LinearYMotion,
		FName LinearZMotion,
		float LinearLimitCm,
		bool bUnilateralDistanceSpring,
		float DistanceSpringRestLengthCm,
		float DistanceSpringStiffnessNPerM,
		float DistanceSpringDampingNsPerM,
		bool bUnilateralPathLengthSpring,
		FVector PathSpringFixedWaypointCm,
		float PathSpringRestLengthCm,
		float PathSpringStiffnessNPerM,
		float PathSpringDampingNsPerM,
		FName AngularXMotion,
		FName AngularYMotion,
		FName AngularZMotion,
		FVector AngularLimitsDegrees,
		FVector LinearPositionDriveEnabled,
		FVector LinearVelocityDriveEnabled,
		FVector LinearPositionTargetCm,
		FVector LinearVelocityTargetCmPerSec,
		FVector LinearStiffness,
		FVector LinearDamping,
		FVector LinearForceLimit,
		bool bLinearAccelerationMode,
		FName AngularDriveMode,
		FVector AngularPositionDriveEnabled,
		FVector AngularVelocityDriveEnabled,
		FVector AngularOrientationTargetDegrees,
		FVector AngularVelocityTargetRevolutionsPerSecond,
		float AngularStiffness,
		float AngularDamping,
		float AngularTorqueLimit,
		bool bAngularAccelerationMode,
		float LinearBreakThreshold,
		float AngularBreakThreshold,
		FName AxialVisualObjectId,
		FName AxialVisualForwardAxis,
		bool bCollisionEnabled);

	UFUNCTION(BlueprintPure, Category = "ADP Physics|Constraints")
	USplineMeshComponent* GetConstraintVisualComponent(FName ConstraintId) const;

	UFUNCTION(BlueprintCallable, Category = "ADP Physics|Constraints")
	void RefreshConstraintVisuals();

	UFUNCTION(BlueprintCallable, Category = "ADP Physics")
	bool StartPreparedCapture();

	UFUNCTION(BlueprintCallable, Category = "ADP Physics")
	void SetManualSteppingEnabled(bool bEnabled);

	UFUNCTION(BlueprintCallable, Category = "ADP Physics")
	void AdvanceCapture(float DeltaSeconds, bool bTickWorld);

	UFUNCTION(BlueprintCallable, Category = "ADP Physics")
	void StopCapture();

	UFUNCTION(BlueprintCallable, Category = "ADP Physics")
	bool WriteCaptureJson(const FString& Path) const;

	UFUNCTION(BlueprintCallable, Category = "ADP Physics")
	FString GetCaptureJson() const;

	UFUNCTION(BlueprintCallable, Category = "ADP Physics")
	bool IsCaptureComplete() const;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	float ContactToleranceCm = 4.0f;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	TArray<FADPDrivenBodyConfig> BodyConfigs;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	TArray<FADPContinuousForceConfig> ContinuousForces;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	TArray<FADPRadialForceConfig> RadialForces;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	TArray<FADPAerodynamicWindConfig> AerodynamicWinds;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	TArray<FADPCompliantContactConfig> CompliantContacts;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	TArray<FADPUnilateralDistanceSpringConfig> UnilateralDistanceSprings;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	TArray<FADPUnilateralPathLengthSpringConfig> UnilateralPathLengthSprings;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	bool bCapturing = false;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	bool bCaptureComplete = false;

private:
	UPROPERTY(Transient)
	TMap<FName, TObjectPtr<APhysicsConstraintActor>> ConstraintActors;
	TSet<FName> BreakableConstraintIds;
	TMap<FName, float> ManagedLinearBreakThresholdsNewton;
	TMap<FName, float> ManagedAngularBreakThresholdsNewtonMeter;
	TSet<FName> ManagedBrokenConstraintIds;
	TMap<FName, FVector> ManagedBreakLinearForceEngineUnits;
	TMap<FName, FVector> ManagedBreakAngularTorqueEngineUnits;

	UPROPERTY(Transient)
	TMap<FName, TObjectPtr<USplineMeshComponent>> ConstraintVisuals;

	TMap<FName, TArray<TObjectPtr<USplineMeshComponent>>> ConstraintVisualExtraSegments;

	UPROPERTY(Transient)
	TMap<FName, TObjectPtr<UPrimitiveComponent>> ConstraintVisualSources;

	TMap<FName, bool> ConstraintVisualSourceVisibility;

	UPROPERTY(Transient)
	TMap<FName, TObjectPtr<UPrimitiveComponent>> ConstraintVisualBodyAs;

	UPROPERTY(Transient)
	TMap<FName, TObjectPtr<UPrimitiveComponent>> ConstraintVisualBodyBs;

	TMap<FName, FTransform> ConstraintVisualLocalFramesA;
	TMap<FName, FTransform> ConstraintVisualLocalFramesB;

	UFUNCTION()
	void HandleComponentHit(
		UPrimitiveComponent* HitComponent,
		AActor* OtherActor,
		UPrimitiveComponent* OtherComponent,
		FVector NormalImpulse,
		const FHitResult& Hit);

	void PrepareBody(const FADPDrivenBodyConfig& Config);
	void ActivateBody(const FADPDrivenBodyConfig& Config);
	void ApplyContinuousForces(float TimeSeconds);
	void ApplyRadialForces(float TimeSeconds);
	void ApplyAerodynamicWinds(float TimeSeconds);
	void CaptureManualFrame(float DeltaSeconds);
	void QueueCompliantContactForces();
	void ApplyCompliantContactSubstep(float DeltaSeconds, FBodyInstance* BodyInstance);
	void QueueUnilateralDistanceSpringForces();
	void ApplyUnilateralDistanceSpringPhysicsStep(float DeltaSeconds, FBodyInstance* BodyInstance);
	void QueueUnilateralPathLengthSpringForces();
	void ApplyUnilateralPathLengthSpringPhysicsStep(float DeltaSeconds, FBodyInstance* BodyInstance);
	void EvaluateManagedConstraintBreaks();
	bool ConfigureAxialVisual(FName ConstraintId, FName ObjectId, FName ForwardAxis);
	void UpdateConstraintVisuals();
	void CaptureFrame();
	UPrimitiveComponent* FindPrimitiveComponent(AActor* Actor) const;
	UPrimitiveComponent* FindRegisteredPrimitive(FName BodyId) const;
	FName FindBodyId(AActor* Actor) const;
	bool ComputeBoundsContact(const FADPDrivenBodyConfig& A, const FADPDrivenBodyConfig& B, FADPContactSample& OutContact) const;
	FString BuildCaptureJson() const;

	float SampleIntervalSeconds = 1.0f / 12.0f;
	float ElapsedSeconds = 0.0f;
	float AccumulatedSeconds = 0.0f;
	int32 MaxFrames = 1;
	int32 NextFrameIndex = 0;
	FString OutputPath;
	TArray<FADPFrameCapture> CapturedFrames;
	TArray<FADPContactSample> PendingNativeContacts;
	FCalculateCustomPhysics CompliantContactDelegate;
	FCalculateCustomPhysics UnilateralDistanceSpringDelegate;
	FCalculateCustomPhysics UnilateralPathLengthSpringDelegate;
	bool bManualSteppingEnabled = false;
	bool bTickingWorldFromDriver = false;
	bool bBodiesPrepared = false;
};
