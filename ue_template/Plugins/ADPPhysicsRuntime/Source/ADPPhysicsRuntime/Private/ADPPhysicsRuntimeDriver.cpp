#include "ADPPhysicsRuntimeDriver.h"

#include "Components/PrimitiveComponent.h"
#include "Components/SplineMeshComponent.h"
#include "Components/StaticMeshComponent.h"
#include "Engine/Engine.h"
#include "Engine/World.h"
#include "JsonObjectConverter.h"
#include "Math/RotationMatrix.h"
#include "Misc/FileHelper.h"
#include "PhysicsEngine/PhysicsConstraintActor.h"
#include "PhysicsEngine/PhysicsConstraintComponent.h"
#include "PhysicsEngine/BodyInstance.h"
#include "Physics/Experimental/PhysInterface_Chaos.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

namespace
{
constexpr int32 ConstraintVisualSegmentCount = 8;

TArray<TSharedPtr<FJsonValue>> VectorToJsonArray(const FVector& Vector)
{
	TArray<TSharedPtr<FJsonValue>> Values;
	Values.Add(MakeShared<FJsonValueNumber>(Vector.X));
	Values.Add(MakeShared<FJsonValueNumber>(Vector.Y));
	Values.Add(MakeShared<FJsonValueNumber>(Vector.Z));
	return Values;
}

TArray<TSharedPtr<FJsonValue>> RotatorToJsonArray(const FRotator& Rotator)
{
	TArray<TSharedPtr<FJsonValue>> Values;
	Values.Add(MakeShared<FJsonValueNumber>(Rotator.Pitch));
	Values.Add(MakeShared<FJsonValueNumber>(Rotator.Yaw));
	Values.Add(MakeShared<FJsonValueNumber>(Rotator.Roll));
	return Values;
}

struct FADPOrientedBox
{
	FVector Center = FVector::ZeroVector;
	FVector Axes[3] = {FVector::ForwardVector, FVector::RightVector, FVector::UpVector};
	FVector Extents = FVector::ZeroVector;
};

bool BuildOrientedBox(AActor* Actor, FADPOrientedBox& OutBox)
{
	if (Actor == nullptr)
	{
		return false;
	}
	UPrimitiveComponent* Primitive = Actor->FindComponentByClass<UPrimitiveComponent>();
	if (Primitive == nullptr)
	{
		return false;
	}
	const FTransform ComponentTransform = Primitive->GetComponentTransform();
	const FBoxSphereBounds LocalBounds = Primitive->CalcBounds(FTransform::Identity);
	const FVector Scale = ComponentTransform.GetScale3D().GetAbs();
	OutBox.Center = ComponentTransform.TransformPosition(LocalBounds.Origin);
	OutBox.Axes[0] = ComponentTransform.GetUnitAxis(EAxis::X);
	OutBox.Axes[1] = ComponentTransform.GetUnitAxis(EAxis::Y);
	OutBox.Axes[2] = ComponentTransform.GetUnitAxis(EAxis::Z);
	OutBox.Extents = LocalBounds.BoxExtent * Scale;
	return !OutBox.Extents.IsNearlyZero();
}

float ProjectedRadius(const FADPOrientedBox& Box, const FVector& Axis)
{
	return
		Box.Extents.X * FMath::Abs(FVector::DotProduct(Box.Axes[0], Axis))
		+ Box.Extents.Y * FMath::Abs(FVector::DotProduct(Box.Axes[1], Axis))
		+ Box.Extents.Z * FMath::Abs(FVector::DotProduct(Box.Axes[2], Axis));
}

bool OrientedBoxSignedMargin(const FADPOrientedBox& A, const FADPOrientedBox& B, float& OutMarginCm)
{
	TArray<FVector, TInlineAllocator<15>> CandidateAxes;
	for (int32 Axis = 0; Axis < 3; ++Axis)
	{
		CandidateAxes.Add(A.Axes[Axis]);
		CandidateAxes.Add(B.Axes[Axis]);
	}
	for (int32 AxisA = 0; AxisA < 3; ++AxisA)
	{
		for (int32 AxisB = 0; AxisB < 3; ++AxisB)
		{
			CandidateAxes.Add(FVector::CrossProduct(A.Axes[AxisA], B.Axes[AxisB]));
		}
	}

	const FVector CenterDelta = B.Center - A.Center;
	float LargestGapCm = -TNumericLimits<float>::Max();
	int32 TestedAxes = 0;
	for (FVector Axis : CandidateAxes)
	{
		if (!Axis.Normalize())
		{
			continue;
		}
		const float CenterDistanceCm = FMath::Abs(FVector::DotProduct(CenterDelta, Axis));
		const float GapCm = CenterDistanceCm - ProjectedRadius(A, Axis) - ProjectedRadius(B, Axis);
		LargestGapCm = FMath::Max(LargestGapCm, GapCm);
		++TestedAxes;
	}
	if (TestedAxes == 0 || !FMath::IsFinite(LargestGapCm))
	{
		return false;
	}
	OutMarginCm = LargestGapCm;
	return true;
}

bool ParseLinearMotion(FName Name, ELinearConstraintMotion& OutMotion)
{
	const FString Value = Name.ToString().ToLower();
	if (Value == TEXT("locked")) { OutMotion = LCM_Locked; return true; }
	if (Value == TEXT("limited")) { OutMotion = LCM_Limited; return true; }
	if (Value == TEXT("free")) { OutMotion = LCM_Free; return true; }
	return false;
}

bool ParseAngularMotion(FName Name, EAngularConstraintMotion& OutMotion)
{
	const FString Value = Name.ToString().ToLower();
	if (Value == TEXT("locked")) { OutMotion = ACM_Locked; return true; }
	if (Value == TEXT("limited")) { OutMotion = ACM_Limited; return true; }
	if (Value == TEXT("free")) { OutMotion = ACM_Free; return true; }
	return false;
}

bool ParseAngularDriveMode(FName Name, bool& bOutEnabled, EAngularDriveMode::Type& OutMode)
{
	const FString Value = Name.ToString().ToLower();
	if (Value.IsEmpty() || Value == TEXT("none"))
	{
		bOutEnabled = false;
		OutMode = EAngularDriveMode::TwistAndSwing;
		return true;
	}
	if (Value == TEXT("twist_and_swing"))
	{
		bOutEnabled = true;
		OutMode = EAngularDriveMode::TwistAndSwing;
		return true;
	}
	if (Value == TEXT("slerp"))
	{
		bOutEnabled = true;
		OutMode = EAngularDriveMode::SLERP;
		return true;
	}
	return false;
}

bool ParseSplineMeshAxis(FName Name, ESplineMeshAxis::Type& OutAxis)
{
	const FString Value = Name.ToString().ToLower();
	if (Value == TEXT("x")) { OutAxis = ESplineMeshAxis::X; return true; }
	if (Value == TEXT("y")) { OutAxis = ESplineMeshAxis::Y; return true; }
	if (Value == TEXT("z")) { OutAxis = ESplineMeshAxis::Z; return true; }
	return false;
}

FTransform MakeConstraintFrame(const FVector& PositionCm, const FVector& PrimaryAxis, const FVector& SecondaryAxis)
{
	return FTransform(
		FRotationMatrix::MakeFromXY(PrimaryAxis.GetSafeNormal(), SecondaryAxis.GetSafeNormal()).ToQuat(),
		PositionCm);
}
}

AADPPhysicsRuntimeDriver::AADPPhysicsRuntimeDriver()
{
	PrimaryActorTick.bCanEverTick = true;
	PrimaryActorTick.bStartWithTickEnabled = true;
	CompliantContactDelegate.BindUObject(this, &AADPPhysicsRuntimeDriver::ApplyCompliantContactSubstep);
	UnilateralDistanceSpringDelegate.BindUObject(this, &AADPPhysicsRuntimeDriver::ApplyUnilateralDistanceSpringPhysicsStep);
}

void AADPPhysicsRuntimeDriver::Tick(float DeltaSeconds)
{
	Super::Tick(DeltaSeconds);

	if (!bCapturing || bManualSteppingEnabled || bTickingWorldFromDriver)
	{
		return;
	}
	ApplyContinuousForces(ElapsedSeconds);
	QueueCompliantContactForces();
	QueueUnilateralDistanceSpringForces();
	UpdateConstraintVisuals();

	ElapsedSeconds += DeltaSeconds;
	AccumulatedSeconds += DeltaSeconds;

	while (bCapturing && (NextFrameIndex == 0 || AccumulatedSeconds >= SampleIntervalSeconds))
	{
		CaptureFrame();
		++NextFrameIndex;
		AccumulatedSeconds = FMath::Max(0.0f, AccumulatedSeconds - SampleIntervalSeconds);

		if (NextFrameIndex >= MaxFrames)
		{
			StopCapture();
		}
	}
}

void AADPPhysicsRuntimeDriver::ResetDriver()
{
	for (const TPair<FName, TObjectPtr<USplineMeshComponent>>& Entry : ConstraintVisuals)
	{
		if (Entry.Value != nullptr)
		{
			Entry.Value->DestroyComponent();
		}
	}
	for (const TPair<FName, TArray<TObjectPtr<USplineMeshComponent>>>& Entry : ConstraintVisualExtraSegments)
	{
		for (USplineMeshComponent* Segment : Entry.Value)
		{
			if (Segment != nullptr)
			{
				Segment->DestroyComponent();
			}
		}
	}
	for (const TPair<FName, TObjectPtr<UPrimitiveComponent>>& Entry : ConstraintVisualSources)
	{
		if (Entry.Value != nullptr)
		{
			Entry.Value->SetVisibility(ConstraintVisualSourceVisibility.FindRef(Entry.Key), true);
		}
	}
	ConstraintVisuals.Reset();
	ConstraintVisualExtraSegments.Reset();
	ConstraintVisualSources.Reset();
	ConstraintVisualSourceVisibility.Reset();
	for (const TPair<FName, TObjectPtr<APhysicsConstraintActor>>& Entry : ConstraintActors)
	{
		if (Entry.Value != nullptr)
		{
			Entry.Value->Destroy();
		}
	}
	ConstraintActors.Reset();
	BodyConfigs.Reset();
	ContinuousForces.Reset();
	CompliantContacts.Reset();
	UnilateralDistanceSprings.Reset();
	CapturedFrames.Reset();
	PendingNativeContacts.Reset();
	OutputPath.Reset();
	ElapsedSeconds = 0.0f;
	AccumulatedSeconds = 0.0f;
	NextFrameIndex = 0;
	MaxFrames = 1;
	bCapturing = false;
	bCaptureComplete = false;
	bBodiesPrepared = false;
}

void AADPPhysicsRuntimeDriver::RegisterBody(
	FName BodyId,
	AActor* Actor,
	float MassKg,
	FVector InitialVelocityCmPerSec,
	FVector InitialImpulseKgCmPerSec,
	bool bEnableGravity,
	float LinearDamping,
	float AngularDamping,
	bool bSimulatePhysics)
{
	if (BodyId.IsNone() || Actor == nullptr)
	{
		return;
	}

	FADPDrivenBodyConfig Config;
	Config.BodyId = BodyId;
	Config.Actor = Actor;
	Config.PrimitiveComponent = FindPrimitiveComponent(Actor);
	Config.bDynamic = true;
	Config.bSimulatePhysics = bSimulatePhysics;
	Config.bEnableGravity = bEnableGravity;
	Config.bCollisionEnabled = true;
	Config.MassKg = FMath::Max(0.001f, MassKg);
	Config.LinearDamping = FMath::Max(0.0f, LinearDamping);
	Config.AngularDamping = FMath::Max(0.0f, AngularDamping);
	Config.InitialVelocityCmPerSec = InitialVelocityCmPerSec;
	Config.InitialImpulseKgCmPerSec = InitialImpulseKgCmPerSec;
	BodyConfigs.Add(Config);
}

void AADPPhysicsRuntimeDriver::RegisterBodyMeters(
	FName BodyId,
	AActor* Actor,
	float MassKg,
	FVector InitialVelocityMetersPerSecond,
	FVector InitialImpulseNewtonSeconds,
	bool bEnableGravity,
	float LinearDamping,
	float AngularDamping,
	bool bSimulatePhysics)
{
	RegisterBody(
		BodyId,
		Actor,
		MassKg,
		InitialVelocityMetersPerSecond * 100.0f,
		InitialImpulseNewtonSeconds * 100.0f,
		bEnableGravity,
		LinearDamping,
		AngularDamping,
		bSimulatePhysics);
}

void AADPPhysicsRuntimeDriver::RegisterBodyMetersWithCollider(
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
	bool bCollisionEnabled)
{
	RegisterBodyMeters(
		BodyId,
		Actor,
		MassKg,
		InitialVelocityMetersPerSecond,
		InitialImpulseNewtonSeconds,
		bEnableGravity,
		LinearDamping,
		AngularDamping,
		bSimulatePhysics);
	if (BodyConfigs.Num() > 0 && BodyConfigs.Last().BodyId == BodyId && BodyConfigs.Last().Actor.Get() == Actor)
	{
		BodyConfigs.Last().ColliderKind = ColliderKind;
		BodyConfigs.Last().bCollisionEnabled = bCollisionEnabled;
	}
}

void AADPPhysicsRuntimeDriver::RegisterStaticBody(FName BodyId, AActor* Actor)
{
	if (BodyId.IsNone() || Actor == nullptr)
	{
		return;
	}

	FADPDrivenBodyConfig Config;
	Config.BodyId = BodyId;
	Config.Actor = Actor;
	Config.PrimitiveComponent = FindPrimitiveComponent(Actor);
	Config.bDynamic = false;
	Config.bSimulatePhysics = false;
	Config.bEnableGravity = false;
	Config.bCollisionEnabled = true;
	BodyConfigs.Add(Config);
}

void AADPPhysicsRuntimeDriver::RegisterStaticBodyWithCollider(FName BodyId, AActor* Actor, FName ColliderKind, bool bCollisionEnabled)
{
	RegisterStaticBody(BodyId, Actor);
	if (BodyConfigs.Num() > 0 && BodyConfigs.Last().BodyId == BodyId && BodyConfigs.Last().Actor.Get() == Actor)
	{
		BodyConfigs.Last().ColliderKind = ColliderKind;
		BodyConfigs.Last().bCollisionEnabled = bCollisionEnabled;
	}
}

bool AADPPhysicsRuntimeDriver::RegisterContinuousForce(
	FName ForceId,
	FName BodyId,
	FVector ForceNewton,
	float StartTimeSeconds,
	float EndTimeSeconds)
{
	if (ForceId.IsNone() || BodyId.IsNone() || ForceNewton.ContainsNaN() || ForceNewton.IsNearlyZero()
		|| !FMath::IsFinite(StartTimeSeconds) || !FMath::IsFinite(EndTimeSeconds)
		|| StartTimeSeconds < 0.0f || EndTimeSeconds <= StartTimeSeconds)
	{
		return false;
	}
	const FADPDrivenBodyConfig* Body = BodyConfigs.FindByPredicate(
		[BodyId](const FADPDrivenBodyConfig& Config) { return Config.BodyId == BodyId && Config.bDynamic; });
	if (Body == nullptr || Body->PrimitiveComponent == nullptr
		|| ContinuousForces.ContainsByPredicate(
			[ForceId](const FADPContinuousForceConfig& Config) { return Config.ForceId == ForceId; }))
	{
		return false;
	}
	FADPContinuousForceConfig Config;
	Config.ForceId = ForceId;
	Config.BodyId = BodyId;
	Config.ForceNewton = ForceNewton;
	Config.StartTimeSeconds = StartTimeSeconds;
	Config.EndTimeSeconds = EndTimeSeconds;
	ContinuousForces.Add(Config);
	return true;
}

bool AADPPhysicsRuntimeDriver::RegisterCompliantContact(
	FName ContactId,
	FName BodyAId,
	FName BodyBId,
	float ActivationDistanceMeters,
	float StiffnessNPerM,
	float DampingNsPerM)
{
	if (ContactId.IsNone() || BodyAId.IsNone() || BodyBId.IsNone() || BodyAId == BodyBId
		|| !FMath::IsFinite(ActivationDistanceMeters) || ActivationDistanceMeters <= 0.0f
		|| !FMath::IsFinite(StiffnessNPerM) || StiffnessNPerM <= 0.0f
		|| !FMath::IsFinite(DampingNsPerM) || DampingNsPerM < 0.0f
		|| CompliantContacts.ContainsByPredicate(
			[ContactId](const FADPCompliantContactConfig& Config) { return Config.ContactId == ContactId; }))
	{
		return false;
	}
	const FADPDrivenBodyConfig* BodyA = BodyConfigs.FindByPredicate(
		[BodyAId](const FADPDrivenBodyConfig& Config) { return Config.BodyId == BodyAId; });
	const FADPDrivenBodyConfig* BodyB = BodyConfigs.FindByPredicate(
		[BodyBId](const FADPDrivenBodyConfig& Config) { return Config.BodyId == BodyBId; });
	if (BodyA == nullptr || BodyB == nullptr || BodyA->PrimitiveComponent == nullptr || BodyB->PrimitiveComponent == nullptr
		|| !BodyA->ColliderKind.ToString().Equals(TEXT("sphere"), ESearchCase::IgnoreCase)
		|| !BodyB->ColliderKind.ToString().Equals(TEXT("sphere"), ESearchCase::IgnoreCase)
		|| (!BodyA->bDynamic && !BodyB->bDynamic))
	{
		return false;
	}
	FADPCompliantContactConfig Config;
	Config.ContactId = ContactId;
	Config.BodyAId = BodyAId;
	Config.BodyBId = BodyBId;
	Config.BodyAComponent = BodyA->PrimitiveComponent;
	Config.BodyBComponent = BodyB->PrimitiveComponent;
	Config.ActivationDistanceCm = ActivationDistanceMeters * 100.0f;
	Config.StiffnessNPerM = StiffnessNPerM;
	Config.DampingNsPerM = DampingNsPerM;
	CompliantContacts.Add(Config);
	return true;
}

void AADPPhysicsRuntimeDriver::StartCapture(float InSampleIntervalSeconds, int32 InMaxFrames, const FString& InOutputPath)
{
	if (PrepareCapture(InSampleIntervalSeconds, InMaxFrames, InOutputPath))
	{
		StartPreparedCapture();
	}
}

bool AADPPhysicsRuntimeDriver::PrepareCapture(float InSampleIntervalSeconds, int32 InMaxFrames, const FString& InOutputPath)
{
	CapturedFrames.Reset();
	PendingNativeContacts.Reset();
	OutputPath = InOutputPath;
	SampleIntervalSeconds = FMath::Max(0.001f, InSampleIntervalSeconds);
	MaxFrames = FMath::Max(1, InMaxFrames);
	ElapsedSeconds = 0.0f;
	AccumulatedSeconds = SampleIntervalSeconds;
	NextFrameIndex = 0;
	bCaptureComplete = false;

	for (const FADPDrivenBodyConfig& Config : BodyConfigs)
	{
		PrepareBody(Config);
	}
	bBodiesPrepared = BodyConfigs.Num() > 0;
	return bBodiesPrepared;
}

APhysicsConstraintActor* AADPPhysicsRuntimeDriver::BindConstraint(
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
	bool bCollisionEnabled)
{
	UWorld* World = GetWorld();
	if (!bBodiesPrepared || World == nullptr || ConstraintId.IsNone() || ConstraintActors.Contains(ConstraintId))
	{
		return nullptr;
	}
	const bool bBodyAWorld = BodyAId.IsNone();
	const bool bBodyBWorld = BodyBId.IsNone();
	UPrimitiveComponent* BodyA = bBodyAWorld ? nullptr : FindRegisteredPrimitive(BodyAId);
	UPrimitiveComponent* BodyB = bBodyBWorld ? nullptr : FindRegisteredPrimitive(BodyBId);
	ELinearConstraintMotion LinearX;
	ELinearConstraintMotion LinearY;
	ELinearConstraintMotion LinearZ;
	EAngularConstraintMotion AngularX;
	EAngularConstraintMotion AngularY;
	EAngularConstraintMotion AngularZ;
	EAngularDriveMode::Type ParsedAngularDriveMode;
	bool bAngularDriveConfigured = false;
	if ((bBodyAWorld && bBodyBWorld)
		|| (!bBodyAWorld && BodyA == nullptr)
		|| (!bBodyBWorld && BodyB == nullptr)
		|| !ParseLinearMotion(LinearXMotion, LinearX)
		|| !ParseLinearMotion(LinearYMotion, LinearY)
		|| !ParseLinearMotion(LinearZMotion, LinearZ)
		|| !ParseAngularMotion(AngularXMotion, AngularX)
		|| !ParseAngularMotion(AngularYMotion, AngularY)
		|| !ParseAngularMotion(AngularZMotion, AngularZ)
		|| !ParseAngularDriveMode(AngularDriveMode, bAngularDriveConfigured, ParsedAngularDriveMode)
		|| (bUnilateralDistanceSpring && (
			!FMath::IsFinite(DistanceSpringRestLengthCm) || DistanceSpringRestLengthCm <= 0.0f
			|| !FMath::IsFinite(DistanceSpringStiffnessNPerM) || DistanceSpringStiffnessNPerM <= 0.0f
			|| !FMath::IsFinite(DistanceSpringDampingNsPerM) || DistanceSpringDampingNsPerM < 0.0f)))
	{
		return nullptr;
	}

	FActorSpawnParameters Params;
	Params.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
	APhysicsConstraintActor* ConstraintActor = World->SpawnActor<APhysicsConstraintActor>(
		APhysicsConstraintActor::StaticClass(), FVector::ZeroVector, FRotator::ZeroRotator, Params);
	UPhysicsConstraintComponent* Component = ConstraintActor != nullptr ? ConstraintActor->GetConstraintComp() : nullptr;
	if (Component == nullptr)
	{
		if (ConstraintActor != nullptr)
		{
			ConstraintActor->Destroy();
		}
		return nullptr;
	}
#if WITH_EDITOR
	ConstraintActor->SetActorLabel(FString::Printf(TEXT("native_phenomena_demo_constraint_%s"), *ConstraintId.ToString()));
#endif
	Component->SetConstrainedComponents(BodyA, NAME_None, BodyB, NAME_None);
	const FTransform FrameA = MakeConstraintFrame(FrameAPositionCm, FrameAPrimaryAxis, FrameASecondaryAxis);
	const FTransform FrameB = MakeConstraintFrame(FrameBPositionCm, FrameBPrimaryAxis, FrameBSecondaryAxis);
	Component->SetConstraintReferenceFrame(EConstraintFrame::Frame1, FrameA);
	Component->SetConstraintReferenceFrame(EConstraintFrame::Frame2, FrameB);
	Component->SetLinearXLimit(LinearX, LinearLimitCm);
	Component->SetLinearYLimit(LinearY, LinearLimitCm);
	Component->SetLinearZLimit(LinearZ, LinearLimitCm);
	Component->SetAngularTwistLimit(AngularX, AngularLimitsDegrees.X);
	Component->SetAngularSwing1Limit(AngularZ, AngularLimitsDegrees.Z);
	Component->SetAngularSwing2Limit(AngularY, AngularLimitsDegrees.Y);
	FConstraintInstance& MutableInstance = Component->ConstraintInstance;
	const bool bLinearPositionX = LinearPositionDriveEnabled.X > 0.5f;
	const bool bLinearPositionY = LinearPositionDriveEnabled.Y > 0.5f;
	const bool bLinearPositionZ = LinearPositionDriveEnabled.Z > 0.5f;
	const bool bLinearVelocityX = LinearVelocityDriveEnabled.X > 0.5f;
	const bool bLinearVelocityY = LinearVelocityDriveEnabled.Y > 0.5f;
	const bool bLinearVelocityZ = LinearVelocityDriveEnabled.Z > 0.5f;
	MutableInstance.SetLinearPositionDrive(bLinearPositionX, bLinearPositionY, bLinearPositionZ);
	MutableInstance.SetLinearVelocityDrive(bLinearVelocityX, bLinearVelocityY, bLinearVelocityZ);
	MutableInstance.SetLinearPositionTarget(LinearPositionTargetCm);
	MutableInstance.SetLinearVelocityTarget(LinearVelocityTargetCmPerSec);
	MutableInstance.SetLinearDriveParams(LinearStiffness, LinearDamping, LinearForceLimit);
	MutableInstance.SetLinearDriveAccelerationMode(bLinearAccelerationMode);
	if (bAngularDriveConfigured)
	{
		MutableInstance.SetAngularDriveMode(ParsedAngularDriveMode);
		if (ParsedAngularDriveMode == EAngularDriveMode::SLERP)
		{
			MutableInstance.SetOrientationDriveSLERP(AngularPositionDriveEnabled.Z > 0.5f);
			MutableInstance.SetAngularVelocityDriveSLERP(AngularVelocityDriveEnabled.Z > 0.5f);
		}
		else
		{
			MutableInstance.SetOrientationDriveTwistAndSwing(AngularPositionDriveEnabled.X > 0.5f, AngularPositionDriveEnabled.Y > 0.5f);
			MutableInstance.SetAngularVelocityDriveTwistAndSwing(AngularVelocityDriveEnabled.X > 0.5f, AngularVelocityDriveEnabled.Y > 0.5f);
		}
		const FRotator OrientationTarget(
			AngularOrientationTargetDegrees.Y,
			AngularOrientationTargetDegrees.Z,
			AngularOrientationTargetDegrees.X);
		MutableInstance.SetAngularOrientationTarget(OrientationTarget.Quaternion());
		MutableInstance.SetAngularVelocityTarget(AngularVelocityTargetRevolutionsPerSecond);
		MutableInstance.SetAngularDriveParams(AngularStiffness, AngularDamping, AngularTorqueLimit);
		MutableInstance.SetAngularDriveAccelerationMode(bAngularAccelerationMode);
	}
	MutableInstance.SetLinearBreakable(LinearBreakThreshold >= 0.0f, FMath::Max(0.0f, LinearBreakThreshold));
	MutableInstance.SetAngularBreakable(AngularBreakThreshold >= 0.0f, FMath::Max(0.0f, AngularBreakThreshold));
	Component->SetDisableCollision(!bCollisionEnabled);

	UPrimitiveComponent* ActualBodyA = nullptr;
	UPrimitiveComponent* ActualBodyB = nullptr;
	FName ActualBoneA;
	FName ActualBoneB;
	Component->GetConstrainedComponents(ActualBodyA, ActualBoneA, ActualBodyB, ActualBoneB);
	const FConstraintInstance& Instance = Component->ConstraintInstance;
	const bool bBodiesVerified = ActualBodyA == BodyA && ActualBodyB == BodyB;
	const bool bInstanceVerified = Instance.IsValidConstraintInstance();
	const bool bMotionsVerified = Instance.GetLinearXMotion() == LinearX
		&& Instance.GetLinearYMotion() == LinearY
		&& Instance.GetLinearZMotion() == LinearZ
		&& Instance.GetAngularTwistMotion() == AngularX
		&& Instance.GetAngularSwing1Motion() == AngularZ
		&& Instance.GetAngularSwing2Motion() == AngularY;
	const bool bFramesVerified = Instance.GetRefFrame(EConstraintFrame::Frame1).Equals(FrameA, 0.001f)
		&& Instance.GetRefFrame(EConstraintFrame::Frame2).Equals(FrameB, 0.001f);
	FVector ActualLinearStiffness;
	FVector ActualLinearDamping;
	FVector ActualLinearForceLimit;
	MutableInstance.GetLinearDriveParams(ActualLinearStiffness, ActualLinearDamping, ActualLinearForceLimit);
	const bool bLinearDriveVerified =
		Instance.IsLinearPositionDriveXEnabled() == bLinearPositionX
		&& Instance.IsLinearPositionDriveYEnabled() == bLinearPositionY
		&& Instance.IsLinearPositionDriveZEnabled() == bLinearPositionZ
		&& Instance.IsLinearVelocityDriveXEnabled() == bLinearVelocityX
		&& Instance.IsLinearVelocityDriveYEnabled() == bLinearVelocityY
		&& Instance.IsLinearVelocityDriveZEnabled() == bLinearVelocityZ
		&& MutableInstance.GetLinearPositionTarget().Equals(LinearPositionTargetCm, 0.001f)
		&& MutableInstance.GetLinearVelocityTarget().Equals(LinearVelocityTargetCmPerSec, 0.001f)
		&& ActualLinearStiffness.Equals(LinearStiffness, 0.001f)
		&& ActualLinearDamping.Equals(LinearDamping, 0.001f)
		&& ActualLinearForceLimit.Equals(LinearForceLimit, 0.001f)
		&& Instance.ProfileInstance.LinearDrive.GetAccelerationMode() == bLinearAccelerationMode;
	bool bAngularDriveVerified = !bAngularDriveConfigured;
	if (bAngularDriveConfigured)
	{
		float ActualAngularStiffness = 0.0f;
		float ActualAngularDamping = 0.0f;
		float ActualAngularTorqueLimit = 0.0f;
		MutableInstance.GetAngularDriveParams(ActualAngularStiffness, ActualAngularDamping, ActualAngularTorqueLimit);
		bool bActualPositionTwist = false;
		bool bActualPositionSwing = false;
		bool bActualVelocityTwist = false;
		bool bActualVelocitySwing = false;
		MutableInstance.GetOrientationDriveTwistAndSwing(bActualPositionTwist, bActualPositionSwing);
		MutableInstance.GetAngularVelocityDriveTwistAndSwing(bActualVelocityTwist, bActualVelocitySwing);
		const FRotator ExpectedOrientation(
			AngularOrientationTargetDegrees.Y,
			AngularOrientationTargetDegrees.Z,
			AngularOrientationTargetDegrees.X);
		bAngularDriveVerified = MutableInstance.GetAngularDriveMode() == ParsedAngularDriveMode
			&& Instance.GetAngularOrientationTarget().Equals(ExpectedOrientation, 0.001f)
			&& Instance.GetAngularVelocityTarget().Equals(AngularVelocityTargetRevolutionsPerSecond, 0.001f)
			&& FMath::IsNearlyEqual(ActualAngularStiffness, AngularStiffness, 0.001f)
			&& FMath::IsNearlyEqual(ActualAngularDamping, AngularDamping, 0.001f)
			&& FMath::IsNearlyEqual(ActualAngularTorqueLimit, AngularTorqueLimit, 0.001f)
			&& Instance.ProfileInstance.AngularDrive.GetAccelerationMode() == bAngularAccelerationMode;
		if (ParsedAngularDriveMode == EAngularDriveMode::SLERP)
		{
			bAngularDriveVerified = bAngularDriveVerified
				&& MutableInstance.GetOrientationDriveSLERP() == (AngularPositionDriveEnabled.Z > 0.5f)
				&& MutableInstance.GetAngularVelocityDriveSLERP() == (AngularVelocityDriveEnabled.Z > 0.5f);
		}
		else
		{
			bAngularDriveVerified = bAngularDriveVerified
				&& bActualPositionTwist == (AngularPositionDriveEnabled.X > 0.5f)
				&& bActualPositionSwing == (AngularPositionDriveEnabled.Y > 0.5f)
				&& bActualVelocityTwist == (AngularVelocityDriveEnabled.X > 0.5f)
				&& bActualVelocitySwing == (AngularVelocityDriveEnabled.Y > 0.5f);
		}
	}
	const bool bBreakThresholdsVerified =
		Instance.IsLinearBreakable() == (LinearBreakThreshold >= 0.0f)
		&& Instance.IsAngularBreakable() == (AngularBreakThreshold >= 0.0f)
		&& (!Instance.IsLinearBreakable() || FMath::IsNearlyEqual(Instance.GetLinearBreakThreshold(), LinearBreakThreshold, 0.001f))
		&& (!Instance.IsAngularBreakable() || FMath::IsNearlyEqual(Instance.GetAngularBreakThreshold(), AngularBreakThreshold, 0.001f));
	bool bSolverFramesVerified = false;
	if (bInstanceVerified)
	{
		const FPhysicsConstraintHandle& Handle = Instance.GetPhysicsConstraintRef();
		bool bSolverFramesMatch = false;
		const bool bSolverFramesRead = FPhysicsInterface::ExecuteRead(Handle, [&](const FPhysicsConstraintHandle& Constraint)
		{
			bSolverFramesMatch = FPhysicsInterface::GetLocalPose(Constraint, EConstraintFrame::Frame1).Equals(FrameA, 0.001f)
				&& FPhysicsInterface::GetLocalPose(Constraint, EConstraintFrame::Frame2).Equals(FrameB, 0.001f);
		});
		bSolverFramesVerified = bSolverFramesRead && bSolverFramesMatch;
	}
	const bool bVerified = bBodiesVerified && bInstanceVerified && bMotionsVerified && bFramesVerified
		&& bLinearDriveVerified && bAngularDriveVerified && bBreakThresholdsVerified && bSolverFramesVerified;
	if (!bVerified)
	{
		UE_LOG(LogTemp, Error, TEXT("ADP constraint bind rejected: id=%s bodies=%d instance=%d motions=%d frames=%d linear_drive=%d angular_drive=%d break=%d solver_frames=%d"),
			*ConstraintId.ToString(), bBodiesVerified, bInstanceVerified, bMotionsVerified, bFramesVerified,
			bLinearDriveVerified, bAngularDriveVerified, bBreakThresholdsVerified, bSolverFramesVerified);
		ConstraintActor->Destroy();
		return nullptr;
	}
	ConstraintActors.Add(ConstraintId, ConstraintActor);
	if (!AxialVisualObjectId.IsNone() && !ConfigureAxialVisual(ConstraintId, AxialVisualObjectId, AxialVisualForwardAxis))
	{
		ConstraintActors.Remove(ConstraintId);
		ConstraintActor->Destroy();
		return nullptr;
	}
	if (bUnilateralDistanceSpring)
	{
		FADPUnilateralDistanceSpringConfig Spring;
		Spring.ConstraintId = ConstraintId;
		Spring.BodyAId = BodyAId;
		Spring.BodyBId = BodyBId;
		Spring.BodyAComponent = BodyA;
		Spring.BodyBComponent = BodyB;
		Spring.bBodyAWorld = bBodyAWorld;
		Spring.bBodyBWorld = bBodyBWorld;
		Spring.FrameAPositionCm = FrameAPositionCm;
		Spring.FrameBPositionCm = FrameBPositionCm;
		Spring.RestLengthCm = DistanceSpringRestLengthCm;
		Spring.StiffnessNPerM = DistanceSpringStiffnessNPerM;
		Spring.DampingNsPerM = DistanceSpringDampingNsPerM;
		Spring.EvaluationCount = MakeShared<FThreadSafeCounter, ESPMode::ThreadSafe>();
		Spring.ActiveEvaluationCount = MakeShared<FThreadSafeCounter, ESPMode::ThreadSafe>();
		UnilateralDistanceSprings.Add(MoveTemp(Spring));
	}
	UpdateConstraintVisuals();
	return ConstraintActor;
}

USplineMeshComponent* AADPPhysicsRuntimeDriver::GetConstraintVisualComponent(FName ConstraintId) const
{
	return ConstraintVisuals.FindRef(ConstraintId);
}

void AADPPhysicsRuntimeDriver::RefreshConstraintVisuals()
{
	UpdateConstraintVisuals();
}

bool AADPPhysicsRuntimeDriver::ConfigureAxialVisual(FName ConstraintId, FName ObjectId, FName ForwardAxis)
{
	ESplineMeshAxis::Type ParsedAxis;
	UStaticMeshComponent* Source = Cast<UStaticMeshComponent>(FindRegisteredPrimitive(ObjectId));
	APhysicsConstraintActor* ConstraintActor = ConstraintActors.FindRef(ConstraintId);
	UPhysicsConstraintComponent* ConstraintComponent = ConstraintActor != nullptr ? ConstraintActor->GetConstraintComp() : nullptr;
	if (!ParseSplineMeshAxis(ForwardAxis, ParsedAxis) || Source == nullptr || Source->GetStaticMesh() == nullptr || ConstraintComponent == nullptr)
	{
		return false;
	}
	UPrimitiveComponent* BodyA = nullptr;
	UPrimitiveComponent* BodyB = nullptr;
	FName BoneA;
	FName BoneB;
	ConstraintComponent->GetConstrainedComponents(BodyA, BoneA, BodyB, BoneB);
	FConstraintInstance& Instance = ConstraintComponent->ConstraintInstance;
	const FTransform FrameA = BodyA != nullptr
		? Instance.GetRefFrame(EConstraintFrame::Frame1) * BodyA->GetComponentTransform()
		: Instance.GetRefFrame(EConstraintFrame::Frame1);
	const FTransform FrameB = BodyB != nullptr
		? Instance.GetRefFrame(EConstraintFrame::Frame2) * BodyB->GetComponentTransform()
		: Instance.GetRefFrame(EConstraintFrame::Frame2);
	if (FrameA.GetLocation().Equals(FrameB.GetLocation(), KINDA_SMALL_NUMBER))
	{
		return false;
	}

	auto CreateSegment = [&]() -> USplineMeshComponent*
	{
		USplineMeshComponent* Segment = NewObject<USplineMeshComponent>(this);
		if (Segment == nullptr)
		{
			return nullptr;
		}
		AddInstanceComponent(Segment);
		Segment->SetMobility(EComponentMobility::Movable);
		Segment->SetStaticMesh(Source->GetStaticMesh());
		Segment->SetForwardAxis(ParsedAxis, false);
		Segment->SetSplineUpDir(ParsedAxis == ESplineMeshAxis::Z ? FVector::YAxisVector : FVector::ZAxisVector, false);
		Segment->SetCollisionEnabled(ECollisionEnabled::NoCollision);
		Segment->SetVisibility(true, true);
		Segment->SetHiddenInGame(false, true);
		for (int32 MaterialIndex = 0; MaterialIndex < Source->GetNumMaterials(); ++MaterialIndex)
		{
			Segment->SetMaterial(MaterialIndex, Source->GetMaterial(MaterialIndex));
		}
		Segment->RegisterComponentWithWorld(GetWorld());
		Segment->SetWorldTransform(Source->GetComponentTransform());
		if (Segment->GetStaticMesh() != Source->GetStaticMesh() || Segment->GetForwardAxis() != ParsedAxis)
		{
			Segment->DestroyComponent();
			return nullptr;
		}
		return Segment;
	};

	USplineMeshComponent* Visual = CreateSegment();
	if (Visual == nullptr)
	{
		return false;
	}
	TArray<TObjectPtr<USplineMeshComponent>> ExtraSegments;
	for (int32 SegmentIndex = 1; SegmentIndex < ConstraintVisualSegmentCount; ++SegmentIndex)
	{
		USplineMeshComponent* Segment = CreateSegment();
		if (Segment == nullptr)
		{
			Visual->DestroyComponent();
			for (USplineMeshComponent* Existing : ExtraSegments)
			{
				Existing->DestroyComponent();
			}
			return false;
		}
		ExtraSegments.Add(Segment);
	}
	ConstraintVisualSourceVisibility.Add(ConstraintId, Source->IsVisible());
	ConstraintVisualSources.Add(ConstraintId, Source);
	ConstraintVisuals.Add(ConstraintId, Visual);
	ConstraintVisualExtraSegments.Add(ConstraintId, MoveTemp(ExtraSegments));
	Source->SetVisibility(false, true);
	return true;
}

void AADPPhysicsRuntimeDriver::UpdateConstraintVisuals()
{
	for (const TPair<FName, TObjectPtr<USplineMeshComponent>>& Entry : ConstraintVisuals)
	{
		USplineMeshComponent* Visual = Entry.Value.Get();
		APhysicsConstraintActor* ConstraintActor = ConstraintActors.FindRef(Entry.Key);
		UPhysicsConstraintComponent* Component = ConstraintActor != nullptr ? ConstraintActor->GetConstraintComp() : nullptr;
		if (Visual == nullptr || Component == nullptr)
		{
			continue;
		}
		FConstraintInstance& Instance = Component->ConstraintInstance;
		UPrimitiveComponent* BodyA = nullptr;
		UPrimitiveComponent* BodyB = nullptr;
		FName BoneA;
		FName BoneB;
		Component->GetConstrainedComponents(BodyA, BoneA, BodyB, BoneB);
		const FTransform LocalFrameA = Instance.GetRefFrame(EConstraintFrame::Frame1);
		const FTransform LocalFrameB = Instance.GetRefFrame(EConstraintFrame::Frame2);
		const FTransform WorldFrameA = BodyA != nullptr ? LocalFrameA * BodyA->GetComponentTransform() : LocalFrameA;
		const FTransform WorldFrameB = BodyB != nullptr ? LocalFrameB * BodyB->GetComponentTransform() : LocalFrameB;
		const FTransform VisualTransform = Visual->GetComponentTransform();
		const FVector StartWorld = WorldFrameA.GetLocation();
		const FVector EndWorld = WorldFrameB.GetLocation();
		const FVector TangentWorld = EndWorld - StartWorld;
		if (!TangentWorld.IsNearlyZero())
		{
			FVector ControlWorld = 0.5f * (StartWorld + EndWorld);
			const FADPUnilateralDistanceSpringConfig* DistanceSpring = UnilateralDistanceSprings.FindByPredicate(
				[&Entry](const FADPUnilateralDistanceSpringConfig& Candidate)
				{
					return Candidate.ConstraintId == Entry.Key;
				});
			const float DistanceCm = TangentWorld.Size();
			if (DistanceSpring != nullptr && DistanceSpring->RestLengthCm > DistanceCm)
			{
				const FVector ChordDirection = TangentWorld / DistanceCm;
				FVector SagDirection = FVector::DownVector - FVector::DotProduct(FVector::DownVector, ChordDirection) * ChordDirection;
				if (!SagDirection.Normalize())
				{
					const FVector FramePrimary = WorldFrameA.GetUnitAxis(EAxis::X);
					SagDirection = FramePrimary - FVector::DotProduct(FramePrimary, ChordDirection) * ChordDirection;
					SagDirection.Normalize();
				}
				const float SagDepthCm = 0.5f * FMath::Sqrt(FMath::Max(
					0.0f,
					FMath::Square(DistanceSpring->RestLengthCm) - FMath::Square(DistanceCm)));
				ControlWorld += 2.0f * SagDepthCm * SagDirection;
			}

			TArray<USplineMeshComponent*> Segments;
			Segments.Add(Visual);
			for (USplineMeshComponent* Segment : ConstraintVisualExtraSegments.FindRef(Entry.Key))
			{
				Segments.Add(Segment);
			}
			auto CurvePoint = [&](float T)
			{
				const float OneMinusT = 1.0f - T;
				return OneMinusT * OneMinusT * StartWorld + 2.0f * OneMinusT * T * ControlWorld + T * T * EndWorld;
			};
			for (int32 SegmentIndex = 0; SegmentIndex < Segments.Num(); ++SegmentIndex)
			{
				USplineMeshComponent* Segment = Segments[SegmentIndex];
				if (Segment == nullptr)
				{
					continue;
				}
				for (int32 MaterialIndex = 0; MaterialIndex < Visual->GetNumMaterials(); ++MaterialIndex)
				{
					Segment->SetMaterial(MaterialIndex, Visual->GetMaterial(MaterialIndex));
				}
				const float StartT = static_cast<float>(SegmentIndex) / static_cast<float>(Segments.Num());
				const float EndT = static_cast<float>(SegmentIndex + 1) / static_cast<float>(Segments.Num());
				const FVector SegmentStartWorld = CurvePoint(StartT);
				const FVector SegmentEndWorld = CurvePoint(EndT);
				const FTransform SegmentTransform = Segment->GetComponentTransform();
				const FVector SegmentStartLocal = SegmentTransform.InverseTransformPosition(SegmentStartWorld);
				const FVector SegmentEndLocal = SegmentTransform.InverseTransformPosition(SegmentEndWorld);
				const FVector SegmentTangentLocal = SegmentTransform.InverseTransformVector(SegmentEndWorld - SegmentStartWorld);
				Segment->SetStartAndEnd(SegmentStartLocal, SegmentTangentLocal, SegmentEndLocal, SegmentTangentLocal, true);
			}
		}
	}
}

bool AADPPhysicsRuntimeDriver::StartPreparedCapture()
{
	if (!bBodiesPrepared || bCapturing)
	{
		return false;
	}
	for (const FADPDrivenBodyConfig& Config : BodyConfigs)
	{
		ActivateBody(Config);
	}
	bCapturing = true;
	return true;
}

void AADPPhysicsRuntimeDriver::SetManualSteppingEnabled(bool bEnabled)
{
	bManualSteppingEnabled = bEnabled;
}

void AADPPhysicsRuntimeDriver::AdvanceCapture(float DeltaSeconds, bool bTickWorld)
{
	if (!bCapturing)
	{
		return;
	}

	const float ClampedDeltaSeconds = FMath::Max(0.0f, DeltaSeconds);
	if (bTickWorld)
	{
		UWorld* World = GetWorld();
		if (World != nullptr && !bTickingWorldFromDriver)
		{
			ApplyContinuousForces(ElapsedSeconds);
			QueueCompliantContactForces();
			QueueUnilateralDistanceSpringForces();
			bTickingWorldFromDriver = true;
			World->Tick(ELevelTick::LEVELTICK_All, ClampedDeltaSeconds);
			bTickingWorldFromDriver = false;
		}
	}

	UpdateConstraintVisuals();
	CaptureManualFrame(ClampedDeltaSeconds);
}

void AADPPhysicsRuntimeDriver::StopCapture()
{
	if (!bCapturing && bCaptureComplete)
	{
		return;
	}

	bCapturing = false;
	bCaptureComplete = true;
	if (!OutputPath.IsEmpty())
	{
		WriteCaptureJson(OutputPath);
	}
}

bool AADPPhysicsRuntimeDriver::WriteCaptureJson(const FString& Path) const
{
	if (Path.IsEmpty())
	{
		return false;
	}
	return FFileHelper::SaveStringToFile(BuildCaptureJson(), *Path);
}

FString AADPPhysicsRuntimeDriver::GetCaptureJson() const
{
	return BuildCaptureJson();
}

bool AADPPhysicsRuntimeDriver::IsCaptureComplete() const
{
	return bCaptureComplete;
}

void AADPPhysicsRuntimeDriver::CaptureManualFrame(float DeltaSeconds)
{
	if (!bCapturing)
	{
		return;
	}

	ElapsedSeconds += DeltaSeconds;
	CaptureFrame();
	++NextFrameIndex;
	if (NextFrameIndex >= MaxFrames)
	{
		StopCapture();
	}
}

void AADPPhysicsRuntimeDriver::PrepareBody(const FADPDrivenBodyConfig& Config)
{
	UPrimitiveComponent* Primitive = Config.PrimitiveComponent.Get();
	if (Primitive == nullptr)
	{
		return;
	}

	Primitive->SetMobility(EComponentMobility::Movable);
	Primitive->SetCollisionProfileName(Config.bDynamic ? FName(TEXT("PhysicsActor")) : FName(TEXT("BlockAll")));
	Primitive->SetCollisionEnabled(Config.bCollisionEnabled ? ECollisionEnabled::QueryAndPhysics : ECollisionEnabled::NoCollision);
	Primitive->SetNotifyRigidBodyCollision(Config.bCollisionEnabled);
	Primitive->OnComponentHit.RemoveDynamic(this, &AADPPhysicsRuntimeDriver::HandleComponentHit);
	if (Config.bCollisionEnabled)
	{
		Primitive->OnComponentHit.AddDynamic(this, &AADPPhysicsRuntimeDriver::HandleComponentHit);
	}
	Primitive->SetEnableGravity(false);

	if (Config.bDynamic && Config.MassKg > 0.0f)
	{
		Primitive->SetMassOverrideInKg(NAME_None, Config.MassKg, true);
	}

	Primitive->SetLinearDamping(Config.LinearDamping);
	Primitive->SetAngularDamping(Config.AngularDamping);
	Primitive->SetSimulatePhysics(Config.bDynamic && Config.bSimulatePhysics);
}

void AADPPhysicsRuntimeDriver::ActivateBody(const FADPDrivenBodyConfig& Config)
{
	UPrimitiveComponent* Primitive = Config.PrimitiveComponent.Get();
	if (Primitive == nullptr)
	{
		return;
	}

	Primitive->SetEnableGravity(Config.bDynamic && Config.bEnableGravity);
	if (Config.bDynamic && Config.bSimulatePhysics)
	{
		Primitive->SetPhysicsLinearVelocity(Config.InitialVelocityCmPerSec, false, NAME_None);
		if (!Config.InitialImpulseKgCmPerSec.IsNearlyZero())
		{
			Primitive->AddImpulse(Config.InitialImpulseKgCmPerSec, NAME_None, false);
		}
		Primitive->WakeAllRigidBodies();
	}
}

void AADPPhysicsRuntimeDriver::ApplyContinuousForces(float TimeSeconds)
{
	for (const FADPContinuousForceConfig& Force : ContinuousForces)
	{
		if (TimeSeconds + KINDA_SMALL_NUMBER < Force.StartTimeSeconds || TimeSeconds >= Force.EndTimeSeconds)
		{
			continue;
		}
		UPrimitiveComponent* Primitive = FindRegisteredPrimitive(Force.BodyId);
		if (Primitive == nullptr || !Primitive->IsSimulatingPhysics())
		{
			continue;
		}
		Primitive->AddForce(Force.ForceNewton * 100.0f, NAME_None, false);
		Primitive->WakeAllRigidBodies();
	}
}

void AADPPhysicsRuntimeDriver::QueueCompliantContactForces()
{
	TSet<FBodyInstance*> QueuedBodies;
	for (const FADPCompliantContactConfig& Contact : CompliantContacts)
	{
		UPrimitiveComponent* PrimitiveA = Contact.BodyAComponent.Get();
		UPrimitiveComponent* PrimitiveB = Contact.BodyBComponent.Get();
		FBodyInstance* BodyA = PrimitiveA != nullptr ? PrimitiveA->GetBodyInstance() : nullptr;
		FBodyInstance* BodyB = PrimitiveB != nullptr ? PrimitiveB->GetBodyInstance() : nullptr;
		FBodyInstance* CallbackBody = BodyA != nullptr && BodyA->IsInstanceSimulatingPhysics() ? BodyA : BodyB;
		if (CallbackBody == nullptr || !CallbackBody->IsInstanceSimulatingPhysics() || QueuedBodies.Contains(CallbackBody))
		{
			continue;
		}
		CallbackBody->AddCustomPhysics(CompliantContactDelegate);
		QueuedBodies.Add(CallbackBody);
	}
}

void AADPPhysicsRuntimeDriver::ApplyCompliantContactSubstep(float DeltaSeconds, FBodyInstance* BodyInstance)
{
	if (BodyInstance == nullptr || DeltaSeconds <= 0.0f)
	{
		return;
	}
	for (const FADPCompliantContactConfig& Contact : CompliantContacts)
	{
		UPrimitiveComponent* PrimitiveA = Contact.BodyAComponent.Get();
		UPrimitiveComponent* PrimitiveB = Contact.BodyBComponent.Get();
		FBodyInstance* BodyA = PrimitiveA != nullptr ? PrimitiveA->GetBodyInstance() : nullptr;
		FBodyInstance* BodyB = PrimitiveB != nullptr ? PrimitiveB->GetBodyInstance() : nullptr;
		FBodyInstance* CallbackBody = BodyA != nullptr && BodyA->IsInstanceSimulatingPhysics() ? BodyA : BodyB;
		if (CallbackBody != BodyInstance || BodyA == nullptr || BodyB == nullptr)
		{
			continue;
		}
		const FVector CenterA = BodyA->GetUnrealWorldTransform_AssumesLocked(false, false).GetLocation();
		const FVector CenterB = BodyB->GetUnrealWorldTransform_AssumesLocked(false, false).GetLocation();
		const FVector Delta = CenterB - CenterA;
		const float DistanceCm = Delta.Size();
		const float CompressionCm = Contact.ActivationDistanceCm - DistanceCm;
		if (CompressionCm <= 0.0f || DistanceCm <= KINDA_SMALL_NUMBER)
		{
			continue;
		}
		const FVector Normal = Delta / DistanceCm;
		const FVector RelativeVelocityCmPerSec =
			BodyB->GetUnrealWorldVelocity_AssumesLocked() - BodyA->GetUnrealWorldVelocity_AssumesLocked();
		const float SeparationSpeedMPerSec = FVector::DotProduct(RelativeVelocityCmPerSec, Normal) / 100.0f;
		const float ForceNewton = FMath::Max(
			0.0f,
			Contact.StiffnessNPerM * (CompressionCm / 100.0f)
			- Contact.DampingNsPerM * SeparationSpeedMPerSec);
		if (ForceNewton <= 0.0f)
		{
			continue;
		}
		const FVector ImpulseEngineUnits = Normal * (ForceNewton * DeltaSeconds * 100.0f);
		if (BodyA->IsInstanceSimulatingPhysics())
		{
			BodyA->AddImpulse(-ImpulseEngineUnits, false);
		}
		if (BodyB->IsInstanceSimulatingPhysics())
		{
			BodyB->AddImpulse(ImpulseEngineUnits, false);
		}
	}
}

void AADPPhysicsRuntimeDriver::QueueUnilateralDistanceSpringForces()
{
	TSet<FBodyInstance*> QueuedBodies;
	for (const FADPUnilateralDistanceSpringConfig& Spring : UnilateralDistanceSprings)
	{
		UPrimitiveComponent* PrimitiveA = Spring.BodyAComponent.Get();
		UPrimitiveComponent* PrimitiveB = Spring.BodyBComponent.Get();
		FBodyInstance* BodyA = PrimitiveA != nullptr ? PrimitiveA->GetBodyInstance() : nullptr;
		FBodyInstance* BodyB = PrimitiveB != nullptr ? PrimitiveB->GetBodyInstance() : nullptr;
		FBodyInstance* CallbackBody = BodyA != nullptr && BodyA->IsInstanceSimulatingPhysics() ? BodyA : BodyB;
		if (CallbackBody == nullptr || !CallbackBody->IsInstanceSimulatingPhysics() || QueuedBodies.Contains(CallbackBody))
		{
			continue;
		}
		CallbackBody->AddCustomPhysics(UnilateralDistanceSpringDelegate);
		QueuedBodies.Add(CallbackBody);
	}
}

void AADPPhysicsRuntimeDriver::ApplyUnilateralDistanceSpringPhysicsStep(float DeltaSeconds, FBodyInstance* BodyInstance)
{
	if (BodyInstance == nullptr || DeltaSeconds <= 0.0f)
	{
		return;
	}
	for (FADPUnilateralDistanceSpringConfig& Spring : UnilateralDistanceSprings)
	{
		UPrimitiveComponent* PrimitiveA = Spring.BodyAComponent.Get();
		UPrimitiveComponent* PrimitiveB = Spring.BodyBComponent.Get();
		FBodyInstance* BodyA = PrimitiveA != nullptr ? PrimitiveA->GetBodyInstance() : nullptr;
		FBodyInstance* BodyB = PrimitiveB != nullptr ? PrimitiveB->GetBodyInstance() : nullptr;
		FBodyInstance* CallbackBody = BodyA != nullptr && BodyA->IsInstanceSimulatingPhysics() ? BodyA : BodyB;
		if (CallbackBody != BodyInstance
			|| (!Spring.bBodyAWorld && BodyA == nullptr)
			|| (!Spring.bBodyBWorld && BodyB == nullptr))
		{
			continue;
		}
		if (Spring.EvaluationCount.IsValid())
		{
			Spring.EvaluationCount->Increment();
		}
		const FVector PointA = Spring.bBodyAWorld
			? Spring.FrameAPositionCm
			: BodyA->GetUnrealWorldTransform_AssumesLocked(false, false).TransformPosition(Spring.FrameAPositionCm);
		const FVector PointB = Spring.bBodyBWorld
			? Spring.FrameBPositionCm
			: BodyB->GetUnrealWorldTransform_AssumesLocked(false, false).TransformPosition(Spring.FrameBPositionCm);
		const FVector Delta = PointB - PointA;
		const float DistanceCm = Delta.Size();
		const float ExtensionCm = DistanceCm - Spring.RestLengthCm;
		Spring.LastDistanceCm = DistanceCm;
		Spring.LastExtensionCm = FMath::Max(0.0f, ExtensionCm);
		Spring.LastSeparationSpeedCmPerSec = 0.0f;
		Spring.LastTensionNewton = 0.0f;
		Spring.LastDirectionAToB = FVector::ZeroVector;
		Spring.LastForceOnBodyBNewton = FVector::ZeroVector;
		if (ExtensionCm <= 0.0f || DistanceCm <= KINDA_SMALL_NUMBER)
		{
			continue;
		}
		const FVector DirectionAToB = Delta / DistanceCm;
		Spring.LastDirectionAToB = DirectionAToB;
		const FVector VelocityA = Spring.bBodyAWorld
			? FVector::ZeroVector
			: BodyA->GetUnrealWorldVelocityAtPoint_AssumesLocked(PointA);
		const FVector VelocityB = Spring.bBodyBWorld
			? FVector::ZeroVector
			: BodyB->GetUnrealWorldVelocityAtPoint_AssumesLocked(PointB);
		const float SeparationSpeedMPerSec = FVector::DotProduct(VelocityB - VelocityA, DirectionAToB) / 100.0f;
		Spring.LastSeparationSpeedCmPerSec = SeparationSpeedMPerSec * 100.0f;
		const float TensionNewton = FMath::Max(
			0.0f,
			Spring.StiffnessNPerM * (ExtensionCm / 100.0f)
			+ Spring.DampingNsPerM * SeparationSpeedMPerSec);
		if (TensionNewton <= 0.0f)
		{
			continue;
		}
		Spring.LastTensionNewton = TensionNewton;
		Spring.LastForceOnBodyBNewton = -DirectionAToB * TensionNewton;
		if (Spring.ActiveEvaluationCount.IsValid())
		{
			Spring.ActiveEvaluationCount->Increment();
		}
		const FVector ImpulseOnBEngineUnits = -DirectionAToB * (TensionNewton * DeltaSeconds * 100.0f);
		if (BodyA != nullptr && BodyA->IsInstanceSimulatingPhysics())
		{
			BodyA->AddImpulseAtPosition(-ImpulseOnBEngineUnits, PointA);
		}
		if (BodyB != nullptr && BodyB->IsInstanceSimulatingPhysics())
		{
			BodyB->AddImpulseAtPosition(ImpulseOnBEngineUnits, PointB);
		}
	}
}

void AADPPhysicsRuntimeDriver::CaptureFrame()
{
	FADPFrameCapture Frame;
	Frame.FrameIndex = NextFrameIndex;
	Frame.TimeSeconds = ElapsedSeconds;

	for (const FADPDrivenBodyConfig& Config : BodyConfigs)
	{
		AActor* Actor = Config.Actor.Get();
		if (Actor == nullptr)
		{
			continue;
		}

		FADPTransformSample Transform;
		Transform.BodyId = Config.BodyId;
		Transform.FrameIndex = NextFrameIndex;
		Transform.TimeSeconds = ElapsedSeconds;
		Transform.LocationCm = Actor->GetActorLocation();
		Transform.RotationDegrees = Actor->GetActorRotation();

		UPrimitiveComponent* Primitive = Config.PrimitiveComponent.Get();
		if (Primitive != nullptr)
		{
			Transform.VelocityCmPerSec = Primitive->GetPhysicsLinearVelocity(NAME_None);
			Transform.AngularVelocityRadPerSec = Primitive->GetPhysicsAngularVelocityInRadians(NAME_None);
		}

		Frame.Transforms.Add(Transform);
	}

	TSet<FString> NativePairs;
	for (const FADPContactSample& NativeContact : PendingNativeContacts)
	{
		Frame.Contacts.Add(NativeContact);
		FString BodyA = NativeContact.BodyA.ToString();
		FString BodyB = NativeContact.BodyB.ToString();
		if (BodyB < BodyA)
		{
			Swap(BodyA, BodyB);
		}
		NativePairs.Add(BodyA + TEXT("|") + BodyB);
	}
	PendingNativeContacts.Reset();

	for (int32 IndexA = 0; IndexA < BodyConfigs.Num(); ++IndexA)
	{
		for (int32 IndexB = IndexA + 1; IndexB < BodyConfigs.Num(); ++IndexB)
		{
			const FADPDrivenBodyConfig& A = BodyConfigs[IndexA];
			const FADPDrivenBodyConfig& B = BodyConfigs[IndexB];
			if (!A.bCollisionEnabled || !B.bCollisionEnabled || (!A.bDynamic && !B.bDynamic))
			{
				continue;
			}

			FADPContactSample Contact;
			if (ComputeBoundsContact(A, B, Contact))
			{
				FString BodyA = Contact.BodyA.ToString();
				FString BodyB = Contact.BodyB.ToString();
				if (BodyB < BodyA)
				{
					Swap(BodyA, BodyB);
				}
				if (!NativePairs.Contains(BodyA + TEXT("|") + BodyB))
				{
					Frame.Contacts.Add(Contact);
				}
			}
		}
	}

	for (const TPair<FName, TObjectPtr<APhysicsConstraintActor>>& Entry : ConstraintActors)
	{
		APhysicsConstraintActor* ConstraintActor = Entry.Value.Get();
		UPhysicsConstraintComponent* Component = ConstraintActor != nullptr ? ConstraintActor->GetConstraintComp() : nullptr;
		if (Component == nullptr)
		{
			continue;
		}
		FADPConstraintSample Sample;
		Sample.ConstraintId = Entry.Key;
		FConstraintInstance& Instance = Component->ConstraintInstance;
		UPrimitiveComponent* BodyA = nullptr;
		UPrimitiveComponent* BodyB = nullptr;
		FName BoneA;
		FName BoneB;
		Component->GetConstrainedComponents(BodyA, BoneA, BodyB, BoneB);
		const FTransform LocalFrameA = Instance.GetRefFrame(EConstraintFrame::Frame1);
		const FTransform LocalFrameB = Instance.GetRefFrame(EConstraintFrame::Frame2);
		const FTransform WorldFrameA = BodyA != nullptr ? LocalFrameA * BodyA->GetComponentTransform() : LocalFrameA;
		const FTransform WorldFrameB = BodyB != nullptr ? LocalFrameB * BodyB->GetComponentTransform() : LocalFrameB;
		Sample.TranslationCm = WorldFrameA.InverseTransformVectorNoScale(WorldFrameB.GetLocation() - WorldFrameA.GetLocation());
		const FVector VelocityA = BodyA != nullptr ? BodyA->GetPhysicsLinearVelocityAtPoint(WorldFrameA.GetLocation()) : FVector::ZeroVector;
		const FVector VelocityB = BodyB != nullptr ? BodyB->GetPhysicsLinearVelocityAtPoint(WorldFrameB.GetLocation()) : FVector::ZeroVector;
		Sample.RelativeVelocityCmPerSec = WorldFrameA.InverseTransformVectorNoScale(VelocityB - VelocityA);
		FVector LinearForceWorld = FVector::ZeroVector;
		FVector AngularTorqueWorld = FVector::ZeroVector;
		Component->GetConstraintForce(LinearForceWorld, AngularTorqueWorld);
		Sample.LinearForceEngineUnits = WorldFrameA.InverseTransformVectorNoScale(LinearForceWorld);
		Sample.AngularTorqueEngineUnits = WorldFrameA.InverseTransformVectorNoScale(AngularTorqueWorld);
		Sample.PositionTargetCm = Instance.GetLinearPositionTarget();
		FVector DriveDamping;
		FVector DriveForceLimit;
		Instance.GetLinearDriveParams(Sample.Stiffness, DriveDamping, DriveForceLimit);
		const FADPUnilateralDistanceSpringConfig* Spring = UnilateralDistanceSprings.FindByPredicate(
			[&Entry](const FADPUnilateralDistanceSpringConfig& Candidate)
			{
				return Candidate.ConstraintId == Entry.Key;
			});
		if (Spring != nullptr)
		{
			Sample.bUnilateralDistanceSpring = true;
			Sample.DistanceSpringRestLengthCm = Spring->RestLengthCm;
			Sample.DistanceSpringStiffnessNPerM = Spring->StiffnessNPerM;
			Sample.DistanceSpringDampingNsPerM = Spring->DampingNsPerM;
			UPrimitiveComponent* SpringBodyA = Spring->BodyAComponent.Get();
			UPrimitiveComponent* SpringBodyB = Spring->BodyBComponent.Get();
			const FVector PointA = Spring->bBodyAWorld
				? Spring->FrameAPositionCm
				: SpringBodyA->GetComponentTransform().TransformPosition(Spring->FrameAPositionCm);
			const FVector PointB = Spring->bBodyBWorld
				? Spring->FrameBPositionCm
				: SpringBodyB->GetComponentTransform().TransformPosition(Spring->FrameBPositionCm);
			Sample.DistanceSpringEvaluationCount = Spring->EvaluationCount.IsValid() ? Spring->EvaluationCount->GetValue() : 0;
			Sample.DistanceSpringActiveEvaluationCount = Spring->ActiveEvaluationCount.IsValid() ? Spring->ActiveEvaluationCount->GetValue() : 0;
			if (Sample.DistanceSpringEvaluationCount > 0)
			{
				Sample.DistanceSpringDistanceCm = Spring->LastDistanceCm;
				Sample.DistanceSpringExtensionCm = Spring->LastExtensionCm;
				Sample.DistanceSpringSeparationSpeedCmPerSec = Spring->LastSeparationSpeedCmPerSec;
				Sample.DistanceSpringTensionNewton = Spring->LastTensionNewton;
				Sample.DistanceSpringDirectionAToB = Spring->LastDirectionAToB;
				Sample.DistanceSpringForceOnBodyBNewton = Spring->LastForceOnBodyBNewton;
			}
			else
			{
				const FVector Delta = PointB - PointA;
				Sample.DistanceSpringDistanceCm = Delta.Size();
				Sample.DistanceSpringExtensionCm = FMath::Max(0.0f, Sample.DistanceSpringDistanceCm - Spring->RestLengthCm);
				Sample.DistanceSpringDirectionAToB = Sample.DistanceSpringDistanceCm > KINDA_SMALL_NUMBER
					? Delta / Sample.DistanceSpringDistanceCm
					: FVector::ZeroVector;
			}
		}
		Sample.bBroken = Component->IsBroken();
		Frame.Constraints.Add(Sample);
	}
	for (const FADPContinuousForceConfig& Force : ContinuousForces)
	{
		if (ElapsedSeconds + KINDA_SMALL_NUMBER >= Force.StartTimeSeconds && ElapsedSeconds <= Force.EndTimeSeconds + KINDA_SMALL_NUMBER)
		{
			Frame.ActiveForces.Add(Force);
		}
	}

	CapturedFrames.Add(Frame);
}

void AADPPhysicsRuntimeDriver::HandleComponentHit(
	UPrimitiveComponent* HitComponent,
	AActor* OtherActor,
	UPrimitiveComponent* OtherComponent,
	FVector NormalImpulse,
	const FHitResult& Hit)
{
	if (!bCapturing || HitComponent == nullptr || OtherActor == nullptr)
	{
		return;
	}
	const FName BodyA = FindBodyId(HitComponent->GetOwner());
	const FName BodyB = FindBodyId(OtherActor);
	if (BodyA.IsNone() || BodyB.IsNone() || BodyA == BodyB)
	{
		return;
	}

	FADPContactSample Contact;
	Contact.FrameIndex = NextFrameIndex;
	Contact.TimeSeconds = ElapsedSeconds;
	Contact.BodyA = BodyA;
	Contact.BodyB = BodyB;
	Contact.bNativeCollision = true;
	Contact.NormalImpulseNs = NormalImpulse.Size() / 100.0f;
	Contact.ImpactPointCm = Hit.ImpactPoint;
	Contact.ImpactNormal = Hit.ImpactNormal;
	for (FADPContactSample& Existing : PendingNativeContacts)
	{
		if ((Existing.BodyA == BodyA && Existing.BodyB == BodyB) || (Existing.BodyA == BodyB && Existing.BodyB == BodyA))
		{
			if (Contact.NormalImpulseNs > Existing.NormalImpulseNs)
			{
				Existing = Contact;
			}
			return;
		}
	}
	PendingNativeContacts.Add(Contact);
}

UPrimitiveComponent* AADPPhysicsRuntimeDriver::FindPrimitiveComponent(AActor* Actor) const
{
	if (Actor == nullptr)
	{
		return nullptr;
	}
	if (UPrimitiveComponent* RootPrimitive = Cast<UPrimitiveComponent>(Actor->GetRootComponent()))
	{
		return RootPrimitive;
	}
	return Actor->FindComponentByClass<UPrimitiveComponent>();
}

UPrimitiveComponent* AADPPhysicsRuntimeDriver::FindRegisteredPrimitive(FName BodyId) const
{
	for (const FADPDrivenBodyConfig& Config : BodyConfigs)
	{
		if (Config.BodyId == BodyId)
		{
			return Config.PrimitiveComponent.Get();
		}
	}
	return nullptr;
}

FName AADPPhysicsRuntimeDriver::FindBodyId(AActor* Actor) const
{
	for (const FADPDrivenBodyConfig& Config : BodyConfigs)
	{
		if (Config.Actor.Get() == Actor)
		{
			return Config.BodyId;
		}
	}
	return NAME_None;
}

bool AADPPhysicsRuntimeDriver::ComputeBoundsContact(const FADPDrivenBodyConfig& A, const FADPDrivenBodyConfig& B, FADPContactSample& OutContact) const
{
	AActor* ActorA = A.Actor.Get();
	AActor* ActorB = B.Actor.Get();
	if (ActorA == nullptr || ActorB == nullptr)
	{
		return false;
	}
	const FName BoxColliderKind(TEXT("box"));
	if (A.ColliderKind != BoxColliderKind || B.ColliderKind != BoxColliderKind)
	{
		return false;
	}

	FADPOrientedBox BoxA;
	FADPOrientedBox BoxB;
	float SignedMarginCm = 0.0f;
	if (!BuildOrientedBox(ActorA, BoxA) || !BuildOrientedBox(ActorB, BoxB) || !OrientedBoxSignedMargin(BoxA, BoxB, SignedMarginCm))
	{
		return false;
	}
	if (SignedMarginCm > ContactToleranceCm)
	{
		return false;
	}

	const FVector AxisGaps(
		FMath::Abs(BoxA.Center.X - BoxB.Center.X) - (ProjectedRadius(BoxA, FVector::ForwardVector) + ProjectedRadius(BoxB, FVector::ForwardVector)),
		FMath::Abs(BoxA.Center.Y - BoxB.Center.Y) - (ProjectedRadius(BoxA, FVector::RightVector) + ProjectedRadius(BoxB, FVector::RightVector)),
		FMath::Abs(BoxA.Center.Z - BoxB.Center.Z) - (ProjectedRadius(BoxA, FVector::UpVector) + ProjectedRadius(BoxB, FVector::UpVector)));

	OutContact.FrameIndex = NextFrameIndex;
	OutContact.TimeSeconds = ElapsedSeconds;
	OutContact.BodyA = A.BodyId;
	OutContact.BodyB = B.BodyId;
	OutContact.GapCm = SignedMarginCm;
	OutContact.AxisGapsCm = AxisGaps;
	return true;
}

FString AADPPhysicsRuntimeDriver::BuildCaptureJson() const
{
	TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
	Root->SetStringField(TEXT("driver"), TEXT("ADPPhysicsRuntimeDriver"));
	Root->SetNumberField(TEXT("sample_interval_s"), SampleIntervalSeconds);
	Root->SetNumberField(TEXT("frame_count"), CapturedFrames.Num());
	Root->SetNumberField(TEXT("requested_max_frames"), MaxFrames);
	Root->SetNumberField(TEXT("contact_tolerance_cm"), ContactToleranceCm);
	Root->SetBoolField(TEXT("capture_complete"), bCaptureComplete);

	TArray<TSharedPtr<FJsonValue>> FramesJson;
	for (const FADPFrameCapture& Frame : CapturedFrames)
	{
		TSharedRef<FJsonObject> FrameObject = MakeShared<FJsonObject>();
		FrameObject->SetNumberField(TEXT("frame"), Frame.FrameIndex);
		FrameObject->SetNumberField(TEXT("time"), Frame.TimeSeconds);
		FrameObject->SetStringField(TEXT("source"), TEXT("adp_cpp_runtime_driver"));

		TSharedRef<FJsonObject> ObjectsObject = MakeShared<FJsonObject>();
		for (const FADPTransformSample& Transform : Frame.Transforms)
		{
			TSharedRef<FJsonObject> TransformObject = MakeShared<FJsonObject>();
			TransformObject->SetArrayField(TEXT("position_cm"), VectorToJsonArray(Transform.LocationCm));
			TransformObject->SetArrayField(TEXT("rotation_degrees"), RotatorToJsonArray(Transform.RotationDegrees));
			TransformObject->SetArrayField(TEXT("velocity_cm_s"), VectorToJsonArray(Transform.VelocityCmPerSec));
			TransformObject->SetArrayField(TEXT("angular_velocity_rad_s"), VectorToJsonArray(Transform.AngularVelocityRadPerSec));
			TransformObject->SetStringField(TEXT("source"), TEXT("adp_cpp_runtime_driver"));
			ObjectsObject->SetObjectField(Transform.BodyId.ToString(), TransformObject);
		}
		FrameObject->SetObjectField(TEXT("objects"), ObjectsObject);

		TArray<TSharedPtr<FJsonValue>> ContactsJson;
		for (const FADPContactSample& Contact : Frame.Contacts)
		{
			TSharedRef<FJsonObject> ContactObject = MakeShared<FJsonObject>();
			ContactObject->SetNumberField(TEXT("frame"), Contact.FrameIndex);
			ContactObject->SetNumberField(TEXT("time"), Contact.TimeSeconds);
			TArray<TSharedPtr<FJsonValue>> Bodies;
			Bodies.Add(MakeShared<FJsonValueString>(Contact.BodyA.ToString()));
			Bodies.Add(MakeShared<FJsonValueString>(Contact.BodyB.ToString()));
			ContactObject->SetArrayField(TEXT("objects"), Bodies);
			ContactObject->SetStringField(
				TEXT("method"),
				Contact.bNativeCollision ? TEXT("ue_on_component_hit") : TEXT("adp_cpp_runtime_oriented_box_sat"));
			ContactObject->SetBoolField(TEXT("native_collision"), Contact.bNativeCollision);
			ContactObject->SetNumberField(TEXT("normal_impulse_n_s"), Contact.NormalImpulseNs);
			ContactObject->SetArrayField(TEXT("impact_point_cm"), VectorToJsonArray(Contact.ImpactPointCm));
			ContactObject->SetArrayField(TEXT("impact_normal"), VectorToJsonArray(Contact.ImpactNormal));
			ContactObject->SetNumberField(TEXT("gap_cm"), Contact.GapCm);
			ContactObject->SetNumberField(TEXT("contact_tolerance_cm"), ContactToleranceCm);
			TSharedRef<FJsonObject> AxisObject = MakeShared<FJsonObject>();
			AxisObject->SetNumberField(TEXT("x"), Contact.AxisGapsCm.X);
			AxisObject->SetNumberField(TEXT("y"), Contact.AxisGapsCm.Y);
			AxisObject->SetNumberField(TEXT("z"), Contact.AxisGapsCm.Z);
			ContactObject->SetObjectField(TEXT("axis_gaps_cm"), AxisObject);
			ContactsJson.Add(MakeShared<FJsonValueObject>(ContactObject));
		}
		FrameObject->SetArrayField(TEXT("contacts"), ContactsJson);

		TArray<TSharedPtr<FJsonValue>> ConstraintsJson;
		for (const FADPConstraintSample& Constraint : Frame.Constraints)
		{
			TSharedRef<FJsonObject> ConstraintObject = MakeShared<FJsonObject>();
			ConstraintObject->SetStringField(TEXT("constraint_id"), Constraint.ConstraintId.ToString());
			ConstraintObject->SetArrayField(TEXT("translation_cm"), VectorToJsonArray(Constraint.TranslationCm));
			ConstraintObject->SetArrayField(TEXT("relative_velocity_cm_s"), VectorToJsonArray(Constraint.RelativeVelocityCmPerSec));
			ConstraintObject->SetArrayField(TEXT("linear_force_engine_units"), VectorToJsonArray(Constraint.LinearForceEngineUnits));
			ConstraintObject->SetArrayField(TEXT("angular_torque_engine_units"), VectorToJsonArray(Constraint.AngularTorqueEngineUnits));
			ConstraintObject->SetArrayField(TEXT("position_target_cm"), VectorToJsonArray(Constraint.PositionTargetCm));
			ConstraintObject->SetArrayField(TEXT("stiffness_n_m"), VectorToJsonArray(Constraint.Stiffness));
			ConstraintObject->SetBoolField(TEXT("unilateral_distance_spring_enabled"), Constraint.bUnilateralDistanceSpring);
			ConstraintObject->SetNumberField(TEXT("distance_spring_rest_length_cm"), Constraint.DistanceSpringRestLengthCm);
			ConstraintObject->SetNumberField(TEXT("distance_spring_stiffness_n_m"), Constraint.DistanceSpringStiffnessNPerM);
			ConstraintObject->SetNumberField(TEXT("distance_spring_damping_n_s_m"), Constraint.DistanceSpringDampingNsPerM);
			ConstraintObject->SetNumberField(TEXT("distance_spring_distance_cm"), Constraint.DistanceSpringDistanceCm);
			ConstraintObject->SetNumberField(TEXT("distance_spring_extension_cm"), Constraint.DistanceSpringExtensionCm);
			ConstraintObject->SetNumberField(TEXT("distance_spring_separation_speed_cm_s"), Constraint.DistanceSpringSeparationSpeedCmPerSec);
			ConstraintObject->SetNumberField(TEXT("distance_spring_tension_n"), Constraint.DistanceSpringTensionNewton);
			ConstraintObject->SetArrayField(TEXT("distance_spring_direction_a_to_b"), VectorToJsonArray(Constraint.DistanceSpringDirectionAToB));
			ConstraintObject->SetArrayField(TEXT("distance_spring_force_on_body_b_n"), VectorToJsonArray(Constraint.DistanceSpringForceOnBodyBNewton));
			ConstraintObject->SetNumberField(TEXT("distance_spring_evaluation_count"), Constraint.DistanceSpringEvaluationCount);
			ConstraintObject->SetNumberField(TEXT("distance_spring_active_evaluation_count"), Constraint.DistanceSpringActiveEvaluationCount);
			ConstraintObject->SetBoolField(TEXT("broken"), Constraint.bBroken);
			ConstraintObject->SetStringField(TEXT("source"), TEXT("adp_cpp_runtime_driver"));
			ConstraintsJson.Add(MakeShared<FJsonValueObject>(ConstraintObject));
		}
		FrameObject->SetArrayField(TEXT("constraints"), ConstraintsJson);

		TArray<TSharedPtr<FJsonValue>> ForcesJson;
		for (const FADPContinuousForceConfig& Force : Frame.ActiveForces)
		{
			TSharedRef<FJsonObject> ForceObject = MakeShared<FJsonObject>();
			ForceObject->SetStringField(TEXT("force_id"), Force.ForceId.ToString());
			ForceObject->SetStringField(TEXT("object"), Force.BodyId.ToString());
			ForceObject->SetArrayField(TEXT("vector_n"), VectorToJsonArray(Force.ForceNewton));
			ForceObject->SetNumberField(TEXT("start_time_s"), Force.StartTimeSeconds);
			ForceObject->SetNumberField(TEXT("end_time_s"), Force.EndTimeSeconds);
			ForceObject->SetStringField(TEXT("source"), TEXT("adp_cpp_runtime_driver"));
			ForcesJson.Add(MakeShared<FJsonValueObject>(ForceObject));
		}
		FrameObject->SetArrayField(TEXT("forces"), ForcesJson);
		FramesJson.Add(MakeShared<FJsonValueObject>(FrameObject));
	}
	Root->SetArrayField(TEXT("frames"), FramesJson);

	FString Output;
	TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Output);
	FJsonSerializer::Serialize(Root, Writer);
	return Output;
}
