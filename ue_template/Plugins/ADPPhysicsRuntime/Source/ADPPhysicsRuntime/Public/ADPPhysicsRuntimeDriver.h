#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "ADPPhysicsRuntimeDriver.generated.h"

USTRUCT(BlueprintType)
struct ADPPHYSICSRUNTIME_API FADPDrivenBodyConfig
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	FName BodyId;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	TObjectPtr<AActor> Actor = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	bool bDynamic = true;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	bool bSimulatePhysics = true;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	bool bEnableGravity = true;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "ADP Physics")
	bool bCollisionEnabled = true;

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

struct FADPFrameCapture
{
	int32 FrameIndex = 0;
	float TimeSeconds = 0.0f;
	TArray<FADPTransformSample> Transforms;
	TArray<FADPContactSample> Contacts;
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

	UFUNCTION(BlueprintCallable, Category = "ADP Physics")
	void StartCapture(float InSampleIntervalSeconds, int32 InMaxFrames, const FString& InOutputPath);

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
	bool bCapturing = false;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "ADP Physics")
	bool bCaptureComplete = false;

private:
	UFUNCTION()
	void HandleComponentHit(
		UPrimitiveComponent* HitComponent,
		AActor* OtherActor,
		UPrimitiveComponent* OtherComponent,
		FVector NormalImpulse,
		const FHitResult& Hit);

	void CaptureManualFrame(float DeltaSeconds);
	void ConfigureBody(const FADPDrivenBodyConfig& Config);
	void CaptureFrame();
	UPrimitiveComponent* FindPrimitiveComponent(AActor* Actor) const;
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
	bool bManualSteppingEnabled = false;
	bool bTickingWorldFromDriver = false;
};
