module dut #(
    parameter M = 8,
    parameter N = 8,
    parameter K = 16,
    parameter LANES = 16,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_b_flat,
    input  wire                    in_b_flat_valid,
    output wire                    in_b_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    // -------------------------------------------------------------------------
    // Memories organized by rows to reduce read-mux area
    // A: 2 banks x 8 rows x 128 bits (each row = 16 bytes)
    // B: 2 banks x 16 rows x 64 bits (each row = 8 bytes)
    // -------------------------------------------------------------------------
    reg [K*DATA_W-1:0] A_mem [0:1][0:M-1];
    reg [N*DATA_W-1:0] B_mem [0:1][0:K-1];

    // -------------------------------------------------------------------------
    // Input tracking: deterministic bank selection by total beat count
    // -------------------------------------------------------------------------
    reg [4:0] a_total_beats; // 0..16
    reg [4:0] b_total_beats; // 0..16

    wire       a_wr_bank = a_total_beats[3]; // 0 for beats 0-7, 1 for 8-15
    wire       b_wr_bank = b_total_beats[3];
    wire [2:0] a_wr_idx  = a_total_beats[2:0]; // 0..7 within bank
    wire [2:0] b_wr_idx  = b_total_beats[2:0];

    assign in_a_flat_ready = !a_total_beats[4]; // < 16
    assign in_b_flat_ready = !b_total_beats[4]; // < 16

    always @(posedge clk) begin
        if (!rst_n) begin
            a_total_beats <= 5'd0;
            b_total_beats <= 5'd0;
        end else begin
            if (in_a_flat_valid && in_a_flat_ready) begin
                A_mem[a_wr_bank][a_wr_idx] <= in_a_flat;
                a_total_beats <= a_total_beats + 1;
            end
            if (in_b_flat_valid && in_b_flat_ready) begin
                // Each B beat carries 2 rows (16 elements = 128 bits)
                B_mem[b_wr_bank][b_wr_idx * 2]     <= in_b_flat[0           +: N*DATA_W];
                B_mem[b_wr_bank][b_wr_idx * 2 + 1] <= in_b_flat[N*DATA_W   +: N*DATA_W];
                b_total_beats <= b_total_beats + 1;
            end
        end
    end

    // -------------------------------------------------------------------------
    // Transaction completion flags
    // -------------------------------------------------------------------------
    reg txn1_computed;
    reg txn2_computed;

    wire bank0_ready = (a_total_beats >= 5'd8) && (b_total_beats >= 5'd8) && !txn1_computed;
    wire bank1_ready = (a_total_beats >= 5'd16) && (b_total_beats >= 5'd16) && !txn2_computed;

    // -------------------------------------------------------------------------
    // Compute engine state
    // -------------------------------------------------------------------------
    localparam PHASE_IDLE = 2'd0;
    localparam PHASE0     = 2'd1; // compute first pair (no output)
    localparam PHASE1     = 2'd2; // output prev pair + compute next pair
    localparam PHASE_LAST = 2'd3; // output last pair

    reg [1:0] phase;
    reg       comp_bank;
    reg [2:0] row_base; // 0,2,4,6
    reg [3:0] step;     // 0..15

    reg signed [ACC_W-1:0] acc     [0:2*N-1];
    reg signed [ACC_W-1:0] out_buf [0:2*N-1];

    // -------------------------------------------------------------------------
    // Combinational reads from memory
    // -------------------------------------------------------------------------
    wire [K*DATA_W-1:0] a_row0 = A_mem[comp_bank][row_base];
    wire [K*DATA_W-1:0] a_row1 = A_mem[comp_bank][row_base + 1];
    wire [N*DATA_W-1:0] b_row  = B_mem[comp_bank][step];

    wire signed [DATA_W-1:0] a_val0 = a_row0[step * DATA_W +: DATA_W];
    wire signed [DATA_W-1:0] a_val1 = a_row1[step * DATA_W +: DATA_W];

    // -------------------------------------------------------------------------
    // Multipliers and adders
    // -------------------------------------------------------------------------
    wire signed [ACC_W-1:0] prod     [0:2*N-1];
    wire signed [ACC_W-1:0] next_acc [0:2*N-1];

    genvar g;
    generate
        for (g = 0; g < N; g = g + 1) begin : mult
            wire signed [DATA_W-1:0] b_g = b_row[g * DATA_W +: DATA_W];
            assign prod[g]     = a_val0 * b_g;
            assign prod[N+g]   = a_val1 * b_g;
            assign next_acc[g]   = acc[g]   + prod[g];
            assign next_acc[N+g] = acc[N+g] + prod[N+g];
        end
    endgenerate

    // -------------------------------------------------------------------------
    // Output interface
    // -------------------------------------------------------------------------
    assign out_valid = (phase == PHASE1) || (phase == PHASE_LAST);
    assign out_c     = out_buf[step];

    // -------------------------------------------------------------------------
    // Compute engine
    // -------------------------------------------------------------------------
    integer cj;
    always @(posedge clk) begin
        if (!rst_n) begin
            phase         <= PHASE_IDLE;
            comp_bank     <= 1'b0;
            row_base      <= 3'd0;
            step          <= 4'd0;
            txn1_computed <= 1'b0;
            txn2_computed <= 1'b0;
            for (cj = 0; cj < 2*N; cj = cj + 1) begin
                acc[cj]     <= {ACC_W{1'b0}};
                out_buf[cj] <= {ACC_W{1'b0}};
            end
        end else begin
            case (phase)
                PHASE_IDLE: begin
                    if (bank0_ready) begin
                        phase     <= PHASE0;
                        comp_bank <= 1'b0;
                        row_base  <= 3'd0;
                        step      <= 4'd0;
                        for (cj = 0; cj < 2*N; cj = cj + 1)
                            acc[cj] <= {ACC_W{1'b0}};
                    end else if (bank1_ready) begin
                        phase     <= PHASE0;
                        comp_bank <= 1'b1;
                        row_base  <= 3'd0;
                        step      <= 4'd0;
                        for (cj = 0; cj < 2*N; cj = cj + 1)
                            acc[cj] <= {ACC_W{1'b0}};
                    end
                end

                PHASE0: begin
                    // Always advance compute
                    for (cj = 0; cj < 2*N; cj = cj + 1) begin
                        acc[cj] <= next_acc[cj];
                    end
                    if (step == K-1) begin
                        for (cj = 0; cj < 2*N; cj = cj + 1)
                            out_buf[cj] <= next_acc[cj];
                        for (cj = 0; cj < 2*N; cj = cj + 1)
                            acc[cj] <= {ACC_W{1'b0}};
                        step     <= 4'd0;
                        row_base <= 3'd2;
                        phase    <= PHASE1;
                    end else begin
                        step <= step + 1;
                    end
                end

                PHASE1: begin
                    if (out_ready) begin
                        for (cj = 0; cj < 2*N; cj = cj + 1) begin
                            acc[cj] <= next_acc[cj];
                        end
                        if (step == K-1) begin
                            for (cj = 0; cj < 2*N; cj = cj + 1)
                                out_buf[cj] <= next_acc[cj];
                            for (cj = 0; cj < 2*N; cj = cj + 1)
                                acc[cj] <= {ACC_W{1'b0}};
                            step <= 4'd0;
                            if (row_base == M-2) begin
                                phase <= PHASE_LAST;
                            end else begin
                                row_base <= row_base + 2;
                            end
                        end else begin
                            step <= step + 1;
                        end
                    end
                end

                PHASE_LAST: begin
                    if (out_ready) begin
                        if (step == 2*N-1) begin
                            step <= 4'd0;
                            phase <= PHASE_IDLE;
                            if (comp_bank == 1'b0)
                                txn1_computed <= 1'b1;
                            else
                                txn2_computed <= 1'b1;
                        end else begin
                            step <= step + 1;
                        end
                    end
                end
            endcase
        end
    end

endmodule
