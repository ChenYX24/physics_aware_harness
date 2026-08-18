#include "ADPPhysicsRuntimeDriver.h"

#include "Components/PrimitiveComponent.h"
#include "Engine/Engine.h"
#include "Engine/World.h"
#include "JsonObjectConverter.h"
#include "Misc/FileHelper.h"
#include "PhysicsEngine/PhysicsConstraintActor.h"
#include "PhysicsEngine/PhysicsConstraintComponent.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

namespace
{
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
}

AADPPhysicsRuntimeDriver::AADPPhysicsRuntimeDriver()
{
	PrimaryActorTick.bCanEverTick = true;
	PrimaryActorTick.bStartWithTickEnabled = true;
}

void AADPPhysicsRuntimeDriver::Tick(float DeltaSeconds)
{
	Super::Tick(DeltaSeconds);

	if (!bCapturing || bManualSteppingEnabled || bTickingWorldFromDriver)
	{
		return;
	}

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
	for (const TPair<FName, TObjectPtr<APhysicsConstraintActor>>& Entry : ConstraintActors)
	{
		if (Entry.Value != nullptr)
		{
			Entry.Value->Destroy();
		}
	}
	ConstraintActors.Reset();
	BodyConfigs.Reset();
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
	FName AngularXMotion,
	FName AngularYMotion,
	FName AngularZMotion,
	FVector AngularLimitsDegrees,
	bool bCollisionEnabled)
{
	UWorld* World = GetWorld();
	if (!bBodiesPrepared || World == nullptr || ConstraintId.IsNone() || ConstraintActors.Contains(ConstraintId))
	{
		return nullptr;
	}
	UPrimitiveComponent* BodyA = FindRegisteredPrimitive(BodyAId);
	UPrimitiveComponent* BodyB = FindRegisteredPrimitive(BodyBId);
	ELinearConstraintMotion LinearX;
	ELinearConstraintMotion LinearY;
	ELinearConstraintMotion LinearZ;
	EAngularConstraintMotion AngularX;
	EAngularConstraintMotion AngularY;
	EAngularConstraintMotion AngularZ;
	if (BodyA == nullptr || BodyB == nullptr
		|| !ParseLinearMotion(LinearXMotion, LinearX)
		|| !ParseLinearMotion(LinearYMotion, LinearY)
		|| !ParseLinearMotion(LinearZMotion, LinearZ)
		|| !ParseAngularMotion(AngularXMotion, AngularX)
		|| !ParseAngularMotion(AngularYMotion, AngularY)
		|| !ParseAngularMotion(AngularZMotion, AngularZ))
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
	Component->SetConstraintReferencePosition(EConstraintFrame::Frame1, FrameAPositionCm);
	Component->SetConstraintReferenceOrientation(EConstraintFrame::Frame1, FrameAPrimaryAxis, FrameASecondaryAxis);
	Component->SetConstraintReferencePosition(EConstraintFrame::Frame2, FrameBPositionCm);
	Component->SetConstraintReferenceOrientation(EConstraintFrame::Frame2, FrameBPrimaryAxis, FrameBSecondaryAxis);
	Component->SetLinearXLimit(LinearX, LinearLimitCm);
	Component->SetLinearYLimit(LinearY, LinearLimitCm);
	Component->SetLinearZLimit(LinearZ, LinearLimitCm);
	Component->SetAngularTwistLimit(AngularX, AngularLimitsDegrees.X);
	Component->SetAngularSwing1Limit(AngularZ, AngularLimitsDegrees.Z);
	Component->SetAngularSwing2Limit(AngularY, AngularLimitsDegrees.Y);
	Component->SetDisableCollision(!bCollisionEnabled);
	Component->TermComponentConstraint();
	Component->InitComponentConstraint();

	UPrimitiveComponent* ActualBodyA = nullptr;
	UPrimitiveComponent* ActualBodyB = nullptr;
	FName ActualBoneA;
	FName ActualBoneB;
	Component->GetConstrainedComponents(ActualBodyA, ActualBoneA, ActualBodyB, ActualBoneB);
	const FConstraintInstance& Instance = Component->ConstraintInstance;
	const auto FrameMatches = [](const FTransform& Actual, const FVector& Position, const FVector& Primary, const FVector& Secondary)
	{
		return Actual.GetTranslation().Equals(Position, 0.01f)
			&& Actual.GetUnitAxis(EAxis::X).Equals(Primary.GetSafeNormal(), 0.001f)
			&& Actual.GetUnitAxis(EAxis::Y).Equals(Secondary.GetSafeNormal(), 0.001f);
	};
	const bool bVerified = ActualBodyA == BodyA
		&& ActualBodyB == BodyB
		&& Instance.IsValidConstraintInstance()
		&& Instance.GetLinearXMotion() == LinearX
		&& Instance.GetLinearYMotion() == LinearY
		&& Instance.GetLinearZMotion() == LinearZ
		&& Instance.GetAngularTwistMotion() == AngularX
		&& Instance.GetAngularSwing1Motion() == AngularZ
		&& Instance.GetAngularSwing2Motion() == AngularY
		&& FrameMatches(Instance.GetRefFrame(EConstraintFrame::Frame1), FrameAPositionCm, FrameAPrimaryAxis, FrameASecondaryAxis)
		&& FrameMatches(Instance.GetRefFrame(EConstraintFrame::Frame2), FrameBPositionCm, FrameBPrimaryAxis, FrameBSecondaryAxis);
	if (!bVerified)
	{
		ConstraintActor->Destroy();
		return nullptr;
	}
	ConstraintActors.Add(ConstraintId, ConstraintActor);
	return ConstraintActor;
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
			bTickingWorldFromDriver = true;
			World->Tick(ELevelTick::LEVELTICK_All, ClampedDeltaSeconds);
			bTickingWorldFromDriver = false;
		}
	}

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
		FramesJson.Add(MakeShared<FJsonValueObject>(FrameObject));
	}
	Root->SetArrayField(TEXT("frames"), FramesJson);

	FString Output;
	TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Output);
	FJsonSerializer::Serialize(Root, Writer);
	return Output;
}
